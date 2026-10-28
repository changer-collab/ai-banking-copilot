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
from time import perf_counter
from typing import Protocol, cast, runtime_checkable

from ai_banking_copilot.planning.call_observation import CallObserver, emit_http_observation
from ai_banking_copilot.planning.model_config import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    ModelConfig,
    NonBlockingLimiter,
)
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.tools.registry import ActionKind as ActionKind
from ai_banking_copilot.tools.registry import UnknownActionError

# `ActionKind` 已于 2026-10-07 **下移**到 `tools/registry.py`
# （`docs/已修复清单-2026-10-08.md` 步骤 1.1）：
# 类别是动作契约的一部分，而 `architecture.md` §6.1 规定「依赖 / 调用方向自上而下、单向」——
# 规划层在上、执行层在下，枚举放执行层才不反向依赖。本模块顶部
# `from ...tools.registry import ActionKind` 即其 re-export，既有引用保持不变。

_SPECS = build_default_registry()


def _kind_of(action: str, kind: ActionKind | None) -> ActionKind:
    """派生时把关：**规范目录里每个动作都必须声明类别**，缺失即拒绝（fail-fast）。

    之所以把 totality 放在这里而不是 `ToolSpec` 构造期：测试需要构造临时 `ToolSpec` 替身，
    不该被强制声明类别；而 02 规范目录是本项目对外词表，缺类别属写错，必须当场拦住。
    """
    if kind is None:
        raise ValueError(f"动作 {action} 未声明 ToolSpec.kind（02 目录须逐动作声明净效应类别）")
    return kind


ACTION_KINDS: Mapping[str, ActionKind] = {
    action: _kind_of(action, _SPECS.get(action).kind) for action in _SPECS.actions()
}
"""规范动作词表（**由 02 目录派生**，见 `docs/已修复清单-2026-10-08.md` 步骤 1.4）：本计划所有模块
（slots / net_effect / kernel）的唯一动作来源。禁止 `bill.query` 等表外动作名。

派生而非手写：新增动作只需在场景模块声明 `ToolSpec.kind`，本表自动跟上——这是「各做各的、
无先后」的前提。绿色纯度不受影响：`ToolSpec` 的不变式已保证 `kind=QUERY ⟺ is_pure_query`。"""

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
    channel: str | None = None


@runtime_checkable
class IntentModel(Protocol):
    """意图模型协议：任何实现都必须可被**不联网**的模板通道替换。"""

    def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
        """把一句自然语言变成意图提案。"""
        ...


_CLAUSE_SPLIT = re.compile(r"[，,。；;、]|然后|接着|再|并且|同时")

_TEMPLATE_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"(?:修改|更改|重置|变更|换)(?:一下)?(?:交易|支付|登录)?密码|密码(?:修改|重置|变更)"),
        "password.change",
        "命中『修改密码』模板",
    ),
    (
        re.compile(r"AA\s*收款|发起收款|让大家还我钱"),
        "transfer.request_collection",
        "命中『AA 收款』模板",
    ),
    (
        re.compile(r"登记\s*(?:预约|定时)转账"),
        "transfer.book",
        "命中『登记预约转账』模板",
    ),
    (re.compile(r"定时|定转|预约转"), "transfer.schedule", "命中『定时转账』模板"),
    (
        # 口语动词：给X转 / 给X打 / 转<数字>。查询与定期出资必须排除，否则会被抢走。
        re.compile(
            r"^(?:(?:请|麻烦|帮我|帮忙|给我|我想|我要"
            r"|今天|明天|后天|下周|下星期|下礼拜|下个?月)[一二三四五六日天]?\s*)*"
            r"(?!(?:每月|按月|定期|固定))"
            r"(?:给[^，,。；;]{1,12}?(?:转账|转|打款|打|汇)"
            r"|转\s*(?:\d|[零一二两三四五六七八九十百千万]|点|些))"
            r"(?![^，,。；;]*(?:记录|流水|明细|账单))"
        ),
        "transfer.create",
        "命中『口语转账』模板",
    ),
    (
        re.compile(
            r"(?:转账|转给|汇款|打款)(?![^，,。；;]*(?:记录|流水|明细|账单))"
        ),
        "transfer.create",
        "命中『转账』模板",
    ),
    (re.compile(r"赎回|卖出理财"), "wealth.redeem", "命中『赎回』模板"),
    (
        re.compile(r"推荐|哪只|哪款|适合我|买哪个|选哪个|比较.{0,8}(产品|理财)"),
        "wealth.list",
        "命中『理财推荐 / 比较』模板（只读，服务端硬筛选）",
    ),
    (re.compile(r"申购|买理财|理财.*买"), "wealth.subscribe", "命中『理财申购』模板"),
    (
        re.compile(r"理财产品|产品对比|产品列表|理财列表|有哪些产品|看看理财"),
        "wealth.list",
        "命中『理财产品列表』模板",
    ),
    (re.compile(r"挂失"), "card.report_lost", "命中『挂失』模板"),
    (re.compile(r"虚拟卡"), "card.apply_virtual", "命中『虚拟卡申请』模板"),
    (re.compile(r"额度|限额"), "card.set_limit", "命中『额度调整』模板"),
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
            r"|什么时候续费|何时续费|扣费提醒|有哪些代扣|识别.{0,4}扣费|在扣费"
        ),
        "subscription.list",
        "命中『订阅列表/续费提醒』模板",
    ),
    (re.compile(r"签约|代扣|自动续费|续费"), "subscription.sign", "命中『代扣签约』模板"),
    (re.compile(r"解冻|解除冻结|释放冻结"), "funds.release", "命中『解冻资金』模板"),
    (re.compile(r"冻结|锁定.*资金|锁.*元"), "funds.freeze", "命中『冻结资金』模板"),
    (
        # 模板只承诺本人余额；不能把未解析的第三人对象替换成本人账户。
        re.compile(
            r"^(?:请|帮我|我想)?\s*(?:查一下|查询|查看|查查|看看|查|看)?\s*"
            r"(?:我|我的|本人|当前)?的?\s*"
            r"(?:账户|帐户|银行卡|储蓄卡|储蓄账户|活期账户|主账户)?\s*"
            r"余额(?:有多少|是多少|还剩多少)?[？?。！!\s]*$"
        ),
        "account.balance",
        "命中『本人余额查询』模板",
    ),
    (
        re.compile(r"年度账单|全年账单|一年的账单|年度消费"),
        "bills.yearly",
        "命中『年度账单』模板",
    ),
    (
        re.compile(r"(?:查看|查询)(?:账单)?分类趋势"),
        "bills.category_trend", "命中『明确分类趋势』模板",
    ),
    (re.compile(r"账单|消费分析|花了多少|消费明细"), "bills.monthly", "命中『账单分析』模板"),
    (
        re.compile(r"异常交易|可疑交易|大额交易|交易记录|交易明细|交易流水|有没有异常|异常流水"),
        "account.transactions",
        "命中『异常交易』模板",
    ),
    # ↓ 生活事件原型 L1–L7（`scenarios/life_events.py`）：**必须留在表尾**——这些是窄词，
    #   只负责把口语送进 `LifeEventsHandler`，绝不能抢上面已冻结的域内规则
    #   （如「锁 2000 元给生日」仍归 `funds.freeze`、「年度账单」仍归 `bills.yearly`）。
    (re.compile(r"生日|寿宴|纪念日|过节"), "merchant.order", "命中『L1 预算锁定』生活事件"),
    (
        re.compile(r"每月给|按月给|定期给|固定出资|每月生活费|按月生活费"),
        "transfer.schedule",
        "命中『L2 定期出资』生活事件",
    ),
    (re.compile(r"出行|出差|旅行|旅游|境外"), "card.set_limit", "命中『L3 出行保障』生活事件"),
    (re.compile(r"凑钱|首付|学费|装修款"), "wealth.redeem", "命中『L4 大额凑集』生活事件"),
    (
        re.compile(r"卡丢了|银行卡丢|卡片丢失|手机丢了|钱包被偷|钱包丢了|疑似盗刷"),
        "card.lock",
        "命中『L5 失卡止损』生活事件",
    ),
    (
        re.compile(r"阶段盘点|年度盘点|年终总结|全年盘点"),
        "bills.yearly",
        "命中『L6 阶段盘点』生活事件",
    ),
    (
        re.compile(r"分摊收款|合租分摊|团购收款|份子钱|AA制分摊"),
        "transfer.request_collection",
        "命中『L7 分摊收款』生活事件",
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
        return IntentProposal(intents=tuple(intents), raw=utterance, channel="template")


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


DEFAULT_LLM_MODEL = DEFAULT_MODEL
"""默认意图模型（可在组合根经 `AI_MODEL` 覆盖）。"""

DEFAULT_LLM_ENDPOINT = DEFAULT_ENDPOINT
"""默认端点（可在组合根经 `AI_BASE_URL` 覆盖）。"""


class LlmIntentModel:
    """真实 LLM 意图通道：可注入、懒加载、默认不参与测试与 CI。"""

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        api_key: str | None = None,
        model: str = DEFAULT_LLM_MODEL,
        endpoint: str = DEFAULT_LLM_ENDPOINT,
        timeout_s: float = 20.0,
        allow_actions: Sequence[str] = CANONICAL_ACTIONS,
        observer: CallObserver | None = None,
        config: ModelConfig | None = None,
        task_id: str | None = None,
        prompt_version: str | None = None,
    ) -> None:
        settings = config
        self._transport = transport
        self._api_key = (
            api_key if api_key is not None
            else settings.api_key if settings is not None
            else os.environ.get("DASHSCOPE_API_KEY")
        )
        self._model = (
            settings.planner_model if settings is not None and model == DEFAULT_LLM_MODEL else model
        )
        self._endpoint = (
            settings.endpoint
            if settings is not None and endpoint == DEFAULT_LLM_ENDPOINT else endpoint
        )
        self._timeout_s = (
            settings.planner_timeout_s if settings is not None and timeout_s == 20.0 else timeout_s
        )
        self._allow_actions = tuple(allow_actions)
        self._observer = observer
        self._limiter = settings.limiter if settings is not None else NonBlockingLimiter(1)
        self._task_id = task_id
        self._prompt_version = prompt_version

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
        except LlmUnavailableError:
            raise
        except Exception as error:
            raise LlmUnavailableError("LLM 调用失败（transport_error）") from error
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
            observer=self._observer,
            limiter=self._limiter,
            task_id=self._task_id,
            prompt_version=self._prompt_version,
        )
        self._transport = transport
        return transport


def _system_prompt(actions: tuple[str, ...]) -> str:
    action_lines: list[str] = []
    for action in actions:
        try:
            description = _SPECS.get(action).description
        except UnknownActionError:
            # `allow_actions` 通常来自规范目录；保留可注入的自定义白名单兼容性，
            # 但不为目录外动作臆造语义。
            description = "未提供动作说明"
        action_lines.append(f"- {action}：{description}")
    action_block = "\n".join(action_lines)
    return (
        "你是银行智能体的意图解析器，只输出 JSON，不解释。\n"
        "可选动作仅限于以下白名单；动作说明是当前执行语义，必须据此区分相似动作：\n"
        f"{action_block}\n"
        "识别规则：\n"
        "1. 只依据用户原文识别意图，按原文出现顺序输出；不补造账户、收款人、金额或日期事实。\n"
        "2. 每个 intent 的 slots.span 必须是用户原文中逐字复制的连续片段；"
        "无法可靠回指原文时不要猜测该意图。\n"
        "3. 每个动作片段须包含支持该动作语义的原文依据；查询不要求出现请求词、金额或日期。"
        "用户明确给出的对应金额或日期必须保留在各自动作片段内，不能裁掉否定、对象限定或条件；"
        "缺少的控制槽交由服务端补全或澄清，模型不得补造。"
        "多个动作不得共享整句，也不得使用重叠片段。只登记预约时不要选择会立即扣款的动作。\n"
        "4. slots 中除 span 外的字段只作未验证线索，不能把模型臆测当成控制事实。\n"
        '输出格式：{"intents":[{"action":"...","confidence":0.0,"evidence":"原文依据",'
        '"slots":{"span":"原文连续片段"}}]}。'
    )


def _load_httpx_transport(
    *, api_key: str, endpoint: str, timeout_s: float, observer: CallObserver | None = None,
    limiter: NonBlockingLimiter | None = None, task_id: str | None = None,
    prompt_version: str | None = None, role: str = "planner",
    channel: str = "llm-strict",
) -> HttpTransport:
    """懒加载可选依赖 httpx；未安装则降级为 `LlmUnavailableError`（H9）。"""
    try:
        module = importlib.import_module("httpx")
    except ImportError as error:
        raise LlmUnavailableError("未安装可选依赖 httpx，真实 LLM 通道不可用") from error
    factory = cast(_HttpClientFactory, module)
    client = factory.Client(timeout=timeout_s)

    def transport(request: str) -> str:
        lease = limiter.try_acquire() if limiter is not None else None
        if limiter is not None and lease is None:
            raise LlmUnavailableError("LLM 调用失败（concurrency_limit）")
        payload = json.loads(request)
        started_at = perf_counter()
        status: int | None = None
        response_text: str | None = None
        error: BaseException | None = None
        try:
            response = client.post(
                endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout_s,
            )
            status = response.status_code
            response_text = response.text
        except BaseException as caught:
            error = caught
            raise
        finally:
            emit_http_observation(
                observer, role=role, started_at=started_at,
                http_status=status, response_text=response_text, error=error,
                model=payload.get("model") if isinstance(payload, dict) else None,
                task_id=task_id, prompt_version=prompt_version,
                attempt_no=1, channel=channel,
            )
            if lease is not None:
                lease.release()
        if response.status_code != 200:
            raise LlmUnavailableError(f"LLM 返回状态 {response.status_code}")
        return _extract_content(response_text)

    return transport


def _extract_content(raw: str) -> str:
    """从 OpenAI 兼容 `/chat/completions` 响应中取出模型输出文本（`choices[0].message.content`）。

    传输层契约是「返回模型文本」，故信封解包发生在此处；注入的测试传输直接返回文本，不受影响。
    """
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LlmUnavailableError("LLM 响应不是合法 JSON（format_error）") from error
    if not isinstance(envelope, dict):
        raise LlmUnavailableError("LLM 响应必须是 JSON 对象")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmUnavailableError("LLM 响应缺少 choices 数组")
    first = choices[0]
    if not isinstance(first, dict):
        raise LlmUnavailableError("LLM choices 元素必须是 JSON 对象")
    message = first.get("message")
    if not isinstance(message, dict):
        raise LlmUnavailableError("LLM choice 缺少 message 对象")
    content = message.get("content")
    if not isinstance(content, str):
        raise LlmUnavailableError("LLM message 缺少 content 文本")
    return content


def _parse_proposal(raw: str, utterance: str, allow_actions: tuple[str, ...]) -> IntentProposal:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LlmUnavailableError("LLM 响应不是合法 JSON（format_error）") from error
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
        span = slots.get("span")
        if not isinstance(span, str) or not span.strip() or span not in utterance:
            raise LlmUnavailableError(
                "LLM intent 缺少可回指原文的 slots.span（span_error）"
            )
        intents.append(
            Intent(
                action=action,
                slots=slots,
                confidence=confidence,
                evidence=str(item.get("evidence", "")),
            )
        )
    return IntentProposal(intents=tuple(intents), raw=utterance, channel="llm-strict")


def _confidence(value: object) -> float:
    try:
        confidence = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise LlmUnavailableError("confidence 非法（format_error）") from error
    if not 0.0 <= confidence <= 1.0:
        raise LlmUnavailableError("confidence 越界（format_error）")
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
            result = self._fallback.propose(utterance, context)
            return IntentProposal(result.intents, result.raw, channel="template_fallback")
        self._degraded = False
        return proposal
