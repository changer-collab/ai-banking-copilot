"""意图模型：把一句话变成**意图提案**（只提案，不产事实、不执行）。

三条纪律：
1. **LLM 输出一律视为提示**：`Intent.slots` 只当线索，**绝不作为控制槽取值**（H1/H3）；
2. **默认通道是模板**：`TemplateIntentModel` 是确定性模板 + 规则，零模型、零网络，
   是 LLM 不可用时的降级通道（H9：声明为能力子集，非等价实现）；
3. **真实通道必须显式启用**：`LlmIntentModel` 需要注入 `transport` 或安装可选依赖 `httpx`，
   且**懒加载**——不在构造期触碰网络。
"""

from __future__ import annotations

import importlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, cast, runtime_checkable


class ActionKind(str, Enum):
    """动作的净效应类别（cf 口径，PRD §5.3）。"""

    QUERY = "query"
    """纯查询：不进 cf。"""

    STATE = "state"
    """状态变更但非不可逆、无资金流：不进 cf。"""

    OUTFLOW = "outflow"
    """流出（正 cf）：转账 / 定时转账 / 理财申购 / 代扣签约 / 自动续费 / 冻结资金。"""

    INFLOW = "inflow"
    """流入（负流出）：理财赎回 / 解冻。"""

    CANCEL_OUTFLOW = "cancel_outflow"
    """取消未来流出（记负）：订阅取消 / 解约。"""

    IRREVERSIBLE = "irreversible"
    """不可逆：挂失——**不入 cf**，单列触发 ≥ 红色 + L3。"""


ACTION_KINDS: Mapping[str, ActionKind] = {
    "account.balance": ActionKind.QUERY,
    "account.transactions": ActionKind.QUERY,
    "bills.monthly": ActionKind.QUERY,
    "bills.category_trend": ActionKind.QUERY,
    "wealth.list": ActionKind.QUERY,
    "card.list": ActionKind.QUERY,
    "subscription.list": ActionKind.QUERY,
    "transfer.create": ActionKind.OUTFLOW,
    "transfer.schedule": ActionKind.OUTFLOW,
    "wealth.subscribe": ActionKind.OUTFLOW,
    "wealth.redeem": ActionKind.INFLOW,
    "subscription.cancel": ActionKind.CANCEL_OUTFLOW,
    "subscription.sign": ActionKind.OUTFLOW,
    "card.lock": ActionKind.STATE,
    "card.unlock": ActionKind.STATE,
    "card.report_lost": ActionKind.IRREVERSIBLE,
    "card.apply_virtual": ActionKind.STATE,
    "card.set_limit": ActionKind.STATE,
    "funds.freeze": ActionKind.OUTFLOW,
    "funds.release": ActionKind.INFLOW,
}
"""规范动作词表（20 项，逐项对齐 `README.md` 规范动作表）：本计划所有模块
（slots / net_effect / kernel）的唯一动作来源。禁止 `bill.query` 等表外动作名。"""

CANONICAL_ACTIONS: tuple[str, ...] = tuple(ACTION_KINDS)


@dataclass(frozen=True)
class Intent:
    """一个候选意图。`slots` 是**提示**（unverified），不是控制槽取值。"""

    action: str
    slots: Mapping[str, object] = field(default_factory=dict)
    confidence: float = 0.0
    evidence: str = ""

    def __post_init__(self) -> None:
        if not self.action:
            raise ValueError("intent.action 不能为空")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("intent.confidence 必须落在 [0, 1]")


@dataclass(frozen=True)
class IntentProposal:
    """一次意图提案：候选意图（按原文子句顺序）+ 原文。"""

    intents: tuple[Intent, ...]
    raw: str


@runtime_checkable
class IntentModel(Protocol):
    """意图模型协议：任何实现都必须可被**不联网**的模板通道替换。"""

    def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
        """把一句自然语言变成意图提案。"""
        ...


_CLAUSE_SPLIT = re.compile(r"[，,。；;、]|然后|接着|再|并且|同时")

_TEMPLATE_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"定时|定转|预约转"), "transfer.schedule", "命中『定时转账』模板"),
    (re.compile(r"转账|转给|汇款|打款"), "transfer.create", "命中『转账』模板"),
    (re.compile(r"赎回|卖出理财"), "wealth.redeem", "命中『赎回』模板"),
    (re.compile(r"申购|买理财|理财.*买"), "wealth.subscribe", "命中『理财申购』模板"),
    (
        re.compile(r"理财产品|产品对比|产品列表|理财列表|有哪些产品|看看理财"),
        "wealth.list",
        "命中『理财产品列表』模板",
    ),
    (re.compile(r"挂失"), "card.report_lost", "命中『挂失』模板"),
    (re.compile(r"解锁|解除.{0,4}?卡"), "card.unlock", "命中『解锁卡片』模板"),
    (re.compile(r"锁.{0,3}卡"), "card.lock", "命中『锁卡』模板（含修饰语）"),
    (re.compile(r"锁卡|限制.*卡|卡.*限制"), "card.lock", "命中『锁卡』模板"),
    (
        re.compile(r"卡片列表|有哪些卡|卡片查询|看看卡片"),
        "card.list",
        "命中『卡片列表』模板",
    ),
    (
        re.compile(r"取消订阅|退订|取消代扣|关掉自动续费"),
        "subscription.cancel",
        "命中『取消订阅』模板",
    ),
    (
        re.compile(
            r"订阅管理|订阅列表|有哪些订阅|订阅查询|续费提醒|到期提醒"
            r"|什么时候续费|何时续费|扣费提醒|有哪些代扣"
        ),
        "subscription.list",
        "命中『订阅列表/续费提醒』模板",
    ),
    (re.compile(r"签约|代扣|自动续费|续费"), "subscription.sign", "命中『代扣签约』模板"),
    (re.compile(r"冻结|锁定.*资金|锁.*元"), "funds.freeze", "命中『冻结资金』模板"),
    (re.compile(r"账单|消费分析|花了多少|消费明细"), "bills.monthly", "命中『账单分析』模板"),
    (
        re.compile(r"异常交易|可疑交易|大额交易|交易记录|交易明细|交易流水|有没有异常|异常流水"),
        "account.transactions",
        "命中『异常交易』模板",
    ),
)
"""规则按**优先级**排列：更具体的规则在前（如"定时"先于"转账"）。"""


class TemplateIntentModel:
    """降级通道：确定性模板 + 规则（零模型、零网络）。"""

    def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
        intents: list[Intent] = []
        for clause in _split_clauses(utterance):
            for pattern, action, evidence in _TEMPLATE_RULES:
                if pattern.search(clause):
                    intents.append(
                        Intent(
                            action=action,
                            slots={"span": clause},
                            confidence=0.9,
                            evidence=evidence,
                        )
                    )
                    break
        return IntentProposal(intents=tuple(intents), raw=utterance)


def _split_clauses(utterance: str) -> tuple[str, ...]:
    """按标点与连接词切子句；只产出**原文子串**（供"可回指"校验）。"""
    parts = [part.strip() for part in _CLAUSE_SPLIT.split(utterance)]
    return tuple(part for part in parts if part)


class LlmUnavailableError(RuntimeError):
    """真实 LLM 通道不可用（缺凭据 / 缺可选依赖 / 调用失败 / 响应不可解析）。

    归一到一个异常类型，是为了让 `FallbackIntentModel` 用**一处**捕获完成降级。
    """


HttpTransport = Callable[[str], str]
"""HTTP 传输抽象：输入请求 JSON 字符串，返回响应 JSON 字符串。测试注入它即完全离线。"""


class _HttpResponse(Protocol):
    status_code: int
    text: str


class _HttpClient(Protocol):
    def post(
        self,
        url: str,
        *,
        json: Mapping[str, object],
        headers: Mapping[str, str],
        timeout: float,
    ) -> _HttpResponse: ...


class _HttpClientFactory(Protocol):
    def Client(self, *, timeout: float) -> _HttpClient: ...


class LlmIntentModel:
    """真实 LLM 意图通道：可注入、懒加载、默认不参与测试与 CI。"""

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        api_key: str | None = None,
        model: str = "qwen-plus",
        endpoint: str = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        timeout_s: float = 20.0,
        allow_actions: Sequence[str] = CANONICAL_ACTIONS,
    ) -> None:
        self._transport = transport
        self._api_key = api_key if api_key is not None else os.environ.get("DASHSCOPE_API_KEY")
        self._model = model
        self._endpoint = endpoint
        self._timeout_s = timeout_s
        self._allow_actions = tuple(allow_actions)

    @property
    def loaded(self) -> bool:
        """是否已具备可用的传输通道（注入或已懒加载）。"""
        return self._transport is not None

    def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
        transport = self._ensure_transport()
        request = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _system_prompt(self._allow_actions)},
                    {"role": "user", "content": utterance},
                ],
            },
            ensure_ascii=False,
        )
        try:
            raw = transport(request)
        except Exception as error:
            raise LlmUnavailableError(f"LLM 调用失败：{error}") from error
        return _parse_proposal(raw, utterance, self._allow_actions)

    def _ensure_transport(self) -> HttpTransport:
        """懒加载：构造期不碰网络；首次 `propose` 才决定用注入通道还是可选依赖。"""
        if self._transport is not None:
            return self._transport
        api_key = self._api_key
        if not api_key:
            raise LlmUnavailableError("缺少 LLM 凭据（环境变量 DASHSCOPE_API_KEY）")
        transport = _load_httpx_transport(
            api_key=api_key,
            endpoint=self._endpoint,
            timeout_s=self._timeout_s,
        )
        self._transport = transport
        return transport


def _system_prompt(actions: tuple[str, ...]) -> str:
    return (
        "你是银行智能体的意图解析器，只输出 JSON，不解释。"
        f"可选动作仅限于：{', '.join(actions)}。"
        '输出格式：{"intents":[{"action":"...","confidence":0.0,"evidence":"..."}]}'
    )


def _load_httpx_transport(*, api_key: str, endpoint: str, timeout_s: float) -> HttpTransport:
    """懒加载可选依赖 httpx；未安装则降级为 `LlmUnavailableError`（H9）。"""
    try:
        module = importlib.import_module("httpx")
    except ImportError as error:
        raise LlmUnavailableError("未安装可选依赖 httpx，真实 LLM 通道不可用") from error
    factory = cast(_HttpClientFactory, module)
    client = factory.Client(timeout=timeout_s)

    def transport(request: str) -> str:
        payload = json.loads(request)
        response = client.post(
            endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )
        if response.status_code != 200:
            raise LlmUnavailableError(f"LLM 返回状态 {response.status_code}")
        return response.text

    return transport


def _parse_proposal(raw: str, utterance: str, allow_actions: tuple[str, ...]) -> IntentProposal:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LlmUnavailableError(f"LLM 响应不是合法 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise LlmUnavailableError("LLM 响应必须是 JSON 对象")
    items = payload.get("intents")
    if not isinstance(items, list):
        raise LlmUnavailableError("LLM 响应缺少 intents 数组")
    intents: list[Intent] = []
    for item in items:
        if not isinstance(item, dict):
            raise LlmUnavailableError("intents 元素必须是 JSON 对象")
        action = str(item.get("action", ""))
        if action not in allow_actions:
            continue
        confidence = _confidence(item.get("confidence", 0.0))
        slots: dict[str, object] = {}
        raw_slots = item.get("slots")
        if isinstance(raw_slots, dict):
            slots = {str(key): value for key, value in raw_slots.items()}
        intents.append(
            Intent(
                action=action,
                slots=slots,
                confidence=confidence,
                evidence=str(item.get("evidence", "")),
            )
        )
    return IntentProposal(intents=tuple(intents), raw=utterance)


def _confidence(value: object) -> float:
    try:
        confidence = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise LlmUnavailableError(f"confidence 非法：{value!r}") from error
    if not 0.0 <= confidence <= 1.0:
        raise LlmUnavailableError(f"confidence 越界：{confidence}")
    return confidence


class FallbackIntentModel:
    """降级组合：主通道不可用即回落模板通道。

    H9 声明：模板通道是**能力子集**（可靠性兜底），**不是等价实现**——
    它只覆盖规范动作的主路径，不处理长尾口语。
    """

    def __init__(self, primary: IntentModel, fallback: IntentModel | None = None) -> None:
        self._primary = primary
        self._fallback = fallback if fallback is not None else TemplateIntentModel()
        self._degraded = False

    @property
    def degraded(self) -> bool:
        """最近一次调用是否发生了降级。"""
        return self._degraded

    def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
        try:
            proposal = self._primary.propose(utterance, context)
        except LlmUnavailableError:
            self._degraded = True
            return self._fallback.propose(utterance, context)
        self._degraded = False
        return proposal
