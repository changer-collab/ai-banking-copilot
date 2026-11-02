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
from ai_banking_copilot.planning.semantic_frame import (
    FrameError,
    SemanticFrame,
    SemanticValue,
    action_for_frame,
    frame_options_for_actions,
    normalize_frame,
    validate_frame,
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
    frames: tuple[SemanticFrame, ...] = ()


@runtime_checkable
class IntentModel(Protocol):
    """意图模型协议：任何实现都必须可被**不联网**的模板通道替换。"""

    def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
        """把一句自然语言变成意图提案。"""
        ...


_CLAUSE_SPLIT = re.compile(r"[，,。；;、]|然后|接着|再|并且|同时")

_TEMPLATE_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(
            r"(?:修改|更改|重置|变更|换)(?:一下)?(?:交易|支付|登录)?密码|密码(?:修改|重置|变更)"
        ),
        "password.change",
        "命中『修改密码』模板",
    ),
    (
        re.compile(r"AA\s*收款|发起收款|让大家还我钱"),
        "transfer.request_collection",
        "命中『AA 收款』模板",
    ),
    (
        # 央行延时到账语义：24 小时延时 / 次日到账必须走「登记未来转账」，登记时不扣款。
        re.compile(
            r"(?!.*(?:记录|流水|明细|账单))"
            r"(?:(?:24\s*小时|二十四小时|延时|延迟|次日到账|明天到账)"
            r".{0,24}(?:转账|转给|汇款|打款)"
            r"|(?:转账|转给|汇款|打款).{0,24}"
            r"(?:24\s*小时|二十四小时|延时|延迟|次日到账|明天到账))"
        ),
        "transfer.book",
        "命中『延时 / 次日转账』模板",
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
        re.compile(r"(?:转账|转给|汇款|打款)(?![^，,。；;]*(?:记录|流水|明细|账单))"),
        "transfer.create",
        "命中『转账』模板",
    ),
    (re.compile(r"赎回|卖出理财"), "wealth.redeem", "命中『赎回』模板"),
    (
        # 「理财到期了，帮我订一束花」：到期 + 花钱动作 = 到期联动（赎回 → 下单）。
        # 必须排在「申购」之前——否则「理财到期帮我买礼物」会被 `理财.*买` 当成申购；
        # `订阅 / 续费 / 提醒` 排除在外，避免抢走订阅域的到期提醒与续订。
        re.compile(
            r"^(?!.*(?:订阅|续费|代扣|提醒)).*(?:到期|满期).{0,16}(?:买|订|下单|送|花|礼物|蛋糕)"
        ),
        "merchant.order",
        "命中『理财到期联动』生活事件（赎回 → 下单）",
    ),
    (
        re.compile(r"推荐|哪只|哪款|适合我|买哪个|选哪个|比较.{0,12}(产品|理财)"),
        "wealth.list",
        "命中『理财推荐 / 比较』模板（只读，服务端硬筛选）",
    ),
    (
        # 理财咨询：只读列举。「闲钱 / 多久不用」只作为**上限**去筛期限与起购，
        # 不排序收益、不预测涨跌（H6）。
        re.compile(r"闲钱|放什么|多久不用|多久之内用|几个月不用|闲置"),
        "wealth.list",
        "命中『闲钱 / 期限咨询』模板（只读；期限与起购只作上限筛）",
    ),
    (
        # 「我想买 R4」这类口述没有「理财」二字：按「买 + 风险等级」识别申购，
        # 否则整句会掉进未知意图，适当性闸门根本没机会先拒。
        re.compile(r"申购|买理财|理财.*买|买.{0,8}R[1-5]"),
        "wealth.subscribe",
        "命中『理财申购』模板",
    ),
    (
        re.compile(r"理财产品|产品对比|产品列表|理财列表|有哪些产品|看看理财"),
        "wealth.list",
        "命中『理财产品列表』模板",
    ),
    (
        # 口语查询：`AIB_DISABLE_LLM=1` 现场演示要能答「我的理财」。**只读**——回的是
        # 产品目录（`wealth.list`），不是持仓查询（当前没有持仓读动作，见验收记录 §4.1）。
        re.compile(r"我的理财|理财持仓|持仓|查.{0,4}理财|理财查询"),
        "wealth.list",
        "命中『我的理财 / 持仓』模板（只读：产品目录）",
    ),
    # 卡片域：先接窄词（临时/正式/补卡/额度类型），再接泛词，避免「额度」吞掉「账单周期额度」。
    (re.compile(r"临时冻结|口头挂失|先冻|暂时冻结"), "card.lock", "命中『临时冻结』模板"),
    (re.compile(r"正式挂失|永久挂失|挂失"), "card.report_lost", "命中『正式挂失』模板"),
    (re.compile(r"补卡|换卡|补办卡|卡损坏"), "card.replace", "命中『补卡』模板"),
    (re.compile(r"临时额度|临时提额"), "card.adjust_credit_limit", "命中『临时额度』模板"),
    (
        re.compile(r"固定额度|永久额度|提高额度|提升额度|提额"),
        "card.adjust_credit_limit",
        "命中『固定额度』模板",
    ),
    (re.compile(r"账单周期|周期消费限额"), "card.set_cycle_limit", "命中『账单周期限额』模板"),
    (re.compile(r"虚拟卡"), "card.apply_virtual", "命中『虚拟卡申请』模板"),
    (re.compile(r"额度|限额"), "card.set_limit", "命中『额度调整』模板"),
    (re.compile(r"解挂|解锁|解除.{0,4}?卡"), "card.unlock", "命中『解挂 / 解锁卡片』模板"),
    (re.compile(r"锁.{0,3}卡"), "card.lock", "命中『锁卡』模板（含修饰语）"),
    (re.compile(r"锁卡|限制.*卡|卡.*限制"), "card.lock", "命中『锁卡』模板"),
    (
        # 口语查询（`AIB_DISABLE_LLM=1` 演示用）：必须带查询动词，否则「了解我的卡片」
        # 这类词会被误判；`(?!.*余额)` 保证「查一下我的银行卡余额」仍归 `account.balance`。
        re.compile(
            r"卡片列表|卡列表|卡片查询|卡片状态|有哪些卡|有多少张卡|看看卡片"
            r"|(?:查询|查看|查一下|查查|看一下|看看|列出)(?:一下)?(?:我|本人)?的?(?:银行|储蓄)?卡"
            r"(?!.*余额)"
        ),
        "card.list",
        "命中『卡片列表』模板",
    ),
    (
        # 取消后的**状态 / 后续扣款**查询：口语里「还扣」「还会扣」「还会继续扣费」都要接住，
        # 因此不写死「还扣」二字，用 `还.{0,3}扣` 覆盖中间的「会 / 会继续」等插入语。
        re.compile(r"取消后商户还扣吗|取消(?:后|以后|之后).*(?:状态|还.{0,3}扣|扣款|扣费)"),
        "subscription.list",
        "命中『取消后状态查询』模板",
    ),
    (
        re.compile(r"(?:不想|不愿|不会|不能|能|能否|是否|可以|能不能).{0,12}(?:取消|退订)"),
        "subscription.list",
        "命中『取消能力/意愿查询』模板",
    ),
    (
        re.compile(
            r"取消订阅|退订|取消代扣|关掉自动续费|关闭扣款协议|关闭代扣协议|解约代扣|代扣解约|关掉银行卡代扣"
            r"|(?:关闭|解约).{0,16}(?:扣款协议|代扣协议)"
            r"|取消.{0,12}(?:会员|订阅|自动续费|代扣)"
            r"|(?:会员|订阅|自动续费|代扣).{0,4}取消"
        ),
        "subscription.cancel",
        "命中『关闭代扣协议』模板",
    ),
    (
        re.compile(
            r"这个月扣了几次|本月扣了几次|涨价了吗|是否涨价|下次什么时候扣|下个月.*订阅.*扣款"
        ),
        "subscription.list",
        "命中『订阅扣费事实查询』模板",
    ),
    (
        re.compile(
            r"订阅管理|订阅列表|有哪些订阅|订阅查询|续费提醒|到期提醒"
            r"|什么时候续费|何时续费|扣费提醒|有哪些代扣|识别.{0,4}扣费|在扣费"
            # 「有哪些自动续费」这类**查看**问法必须先落到列表查询；
            # 否则会被下面的「代扣签约」模板按「自动续费」二字抢走，直接反问商户/金额。
            r"|有哪些自动续费|有没有自动续费|自动续费有哪些|自动续费清单"
            r"|自动续费列表|自动续费查询|最近.{0,6}自动续费"
            r"|(?:查|看|有哪些|多少|我的|管理).{0,6}代扣协议"
            r"|代扣协议.{0,6}(?:查询|列表|清单|管理)"
        ),
        "subscription.list",
        "命中『订阅列表/续费提醒』模板",
    ),
    (
        # 动宾倒装：「把视频会员自动续费关掉」→ 取消，不是查询。
        re.compile(r"(?:自动续费|自动扣款|订阅|会员|代扣).{0,6}(?:关掉|关闭|取消|停掉|解约|关停)"),
        "subscription.cancel",
        "命中『关闭自动续费』模板（动宾倒装）",
    ),
    (
        # Agent 不代办新签约：仅查询已签协议（解约由上面的取消规则承接）。
        re.compile(r"签约|代扣|自动续费|自动扣款|续费"),
        "subscription.list",
        "命中『代扣协议查询』模板",
    ),
    (re.compile(r"解冻|解除冻结|释放冻结"), "funds.release", "命中『解冻资金』模板"),
    (
        # 「先把预算锁了，别让我乱花」是**只冻结、不下单**，必须先于 L1 的「生日」词。
        re.compile(
            r"(?:预算|这笔钱|钱).{0,8}(?:锁|冻结|冻住|留)|先.{0,6}(?:锁|冻)"
            r"|别让我乱花|别乱花|冻住这笔"
        ),
        "funds.freeze",
        "命中『只锁预算』模板",
    ),
    (re.compile(r"冻结|锁定.*资金|锁.*元|先冻"), "funds.freeze", "命中『冻结资金』模板"),
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
        # 指代某笔消费的核实问法（「这笔 6800 是不是我刷的」）：先归到账单域
        # （**不是**账户查询动作），由账单场景统一追问「哪一笔」——不能直接掉进
        # 「未知意图」，那等于系统听不懂人话。
        # 放在转账 / 订阅 / 冻结之后，避免「这笔转账」「取消这笔订阅」被这条抢走。
        re.compile(r"(?:这|那)(?:一)?笔|是不是我刷的|是我刷的"),
        "bills.monthly",
        "命中『指代某笔交易核实』模板（追问问账单场景）",
    ),
    (
        re.compile(r"年度账单|全年账单|一年的账单|年度消费"),
        "bills.yearly",
        "命中『年度账单』模板",
    ),
    (
        re.compile(r"(?:查看|查询)(?:账单)?分类趋势"),
        "bills.category_trend",
        "命中『明确分类趋势』模板",
    ),
    (
        re.compile(r"账单|消费分析|花了多少|花哪了|钱花哪|消费明细"),
        "bills.monthly",
        "命中『账单分析』模板",
    ),
    (
        # 「上周三星巴克 128 元是不是盗刷」：带核实语气的单笔交易核实 = 只读查流水。
        # 必须排在 L5「疑似盗刷」之前，避免被当成挂失；也不抢「我卡被盗刷了，帮我冻结」
        # 这类明确要处置的说法（没有「是不是 / 是否 / 吗」）。
        re.compile(r"(?:是不是|是否|有没有|会不会).{0,8}盗刷|盗刷.{0,6}(?:吗|么|嘛)"),
        "account.transactions",
        "命中『单笔盗刷核实』模板（只读查询）",
    ),
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
    # 不要在这里把「出差 / 出行」单独映射成 `card.set_limit`：逗号切句后会与
    # 「把卡片日限额降到 1000 元」各产生一个同动作意图（两组重复表单）。
    # L3 识别交给 `scenarios.life_events.detect_life_event`。
    (re.compile(r"凑钱|首付|学费|装修款"), "wealth.redeem", "命中『L4 大额凑集』生活事件"),
    (
        re.compile(r"卡丢了|银行卡丢|卡片丢失|手机丢了|钱包被偷|钱包丢了|疑似盗刷"),
        "card.report_lost",
        "命中『L5 失卡止损』生活事件（先问临时冻结还是正式挂失）",
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


_WHOLE_UTTERANCE_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        # 「生日预算的 1000，帮我买个 T+0 理财」：预算只是**资金来源**，
        # 主意图是申购。整句优先于分句的「生日 → merchant.order」，
        # 否则会被 L1 花店/配送澄清抢走。
        re.compile(
            r"(?:预算.{0,24}(?:买|申购|购买).{0,16}(?:T\+0|理财)"
            r"|(?:买|申购|购买).{0,16}(?:T\+0|理财).{0,24}预算)",
            re.IGNORECASE,
        ),
        "wealth.subscribe",
        "命中『预算买理财』模板（整句优先）",
    ),
)
"""整句级规则：必须在分句前命中，避免预算 / 生活事件词抢走主意图。"""


class TemplateIntentModel:
    """降级通道：确定性模板 + 规则（零模型、零网络）。"""

    def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
        for pattern, action, evidence in _WHOLE_UTTERANCE_RULES:
            if pattern.search(utterance):
                return IntentProposal(
                    intents=(
                        Intent(
                            action=action,
                            slots={"span": utterance},
                            confidence=0.9,
                            evidence=evidence,
                        ),
                    ),
                    raw=utterance,
                    channel="template",
                )
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
            api_key
            if api_key is not None
            else settings.api_key
            if settings is not None
            else os.environ.get("DASHSCOPE_API_KEY")
        )
        self._model = (
            settings.planner_model if settings is not None and model == DEFAULT_LLM_MODEL else model
        )
        self._endpoint = (
            settings.endpoint
            if settings is not None and endpoint == DEFAULT_LLM_ENDPOINT
            else endpoint
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
        context_block = json.dumps(
            {
                "today": context.get("today"),
                "recent": context.get("recent", []),
                "last_plan": context.get("last_plan", []),
                "selected": context.get("selected", {}),
            },
            ensure_ascii=False,
            default=str,
        )
        request = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _system_prompt(self._allow_actions)},
                    {
                        "role": "user",
                        "content": (
                            "【会话上下文】只读摘要，仅帮助理解指代，不是新事实；"
                            "槽位仍由服务端解析或用户确认。\n"
                            f"{context_block}\n\n"
                            "【用户原话】\n"
                            f"{utterance}\n\n"
                            "只对【用户原话】识别意图；slots.span 必须逐字摘自【用户原话】。"
                        ),
                    },
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
    frame_lines = []
    for option in frame_options_for_actions(actions):
        roles = ", ".join(sorted(option.allowed_roles)) or "no roles"
        frame_lines.append(
            f"- {option.domain} / {option.operation} / {option.mode} -> {option.action}"
            f" (roles: {roles})"
        )
    frame_block = "\n".join(frame_lines) or "- no registered semantic frame"
    return (
        "你是银行智能体的语义解析器，只输出 JSON，不解释。\n"
        "以下动作白名单仅帮助选择语义，绝不能原样输出动作名：\n"
        f"{action_block}\n"
        "可输出的语义三元组如下；只能从中选择，箭头右侧动作名只供理解、不得输出：\n"
        f"{frame_block}\n"
        "识别规则：\n"
        "1. 只依据用户原文识别语义帧，按原文出现顺序输出；不补造账户、收款人、金额或日期事实。\n"
        "2. 不得输出工具名、对象 ID、账户 ID、卡 ID、产品 ID、订阅 ID、授权决定或验证结果。\n"
        "3. 每个 frame 和 role 的 evidence 必须逐字复制用户原文的连续片段；"
        "无法可靠回指时不要猜测。\n"
        "4. budget_limit 只表示预算上限，绝不是 order_amount、transfer_amount "
        "或 investment_amount。"
        "缺少金额时不得猜测，交由服务端澄清。多个可执行帧不得重叠。\n"
        "5. 会话上下文只用于理解指代；不得把上下文中的金额、商户或日期写进本次 evidence。\n"
        "6. transfer / schedule / schedule 表示用户说“定时转账、定转或预约转”，"
        "会创建按指定时间执行的转账；transfer / book / schedule 仅表示用户明确说“登记预约”。"
        "只说“定时”绝不能选 book。\n"
        '输出格式：{"frames":[{"domain":"transfer","operation":"create","mode":"write",'
        '"evidence":"原文连续片段","confidence":0.0,"roles":{"amount":{"value":50000,'
        '"role":"transfer_amount","evidence":"500元"}}}]}。'
    )


def _load_httpx_transport(
    *,
    api_key: str,
    endpoint: str,
    timeout_s: float,
    observer: CallObserver | None = None,
    limiter: NonBlockingLimiter | None = None,
    task_id: str | None = None,
    prompt_version: str | None = None,
    role: str = "planner",
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
                observer,
                role=role,
                started_at=started_at,
                http_status=status,
                response_text=response_text,
                error=error,
                model=payload.get("model") if isinstance(payload, dict) else None,
                task_id=task_id,
                prompt_version=prompt_version,
                attempt_no=1,
                channel=channel,
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
    frames_payload = payload.get("frames")
    if isinstance(frames_payload, list):
        return _parse_frame_proposal(frames_payload, utterance, allow_actions)
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
            raise LlmUnavailableError("LLM intent 缺少可回指原文的 slots.span（span_error）")
        intents.append(
            Intent(
                action=action,
                slots=slots,
                confidence=confidence,
                evidence=str(item.get("evidence", "")),
            )
        )
    return IntentProposal(intents=tuple(intents), raw=utterance, channel="llm-strict")


def _parse_frame_proposal(
    items: list[object],
    utterance: str,
    allow_actions: tuple[str, ...],
) -> IntentProposal:
    """Parse semantic-only LLM output and derive legacy action hints safely."""
    frames: list[SemanticFrame] = []
    intents: list[Intent] = []
    for item in items:
        if not isinstance(item, dict):
            raise LlmUnavailableError("frames 元素必须是 JSON 对象")
        raw_roles = item.get("roles", {})
        if not isinstance(raw_roles, dict):
            raise LlmUnavailableError("frame roles 必须是 JSON 对象")
        roles: dict[str, SemanticValue] = {}
        for name, raw_value in raw_roles.items():
            if not isinstance(raw_value, dict):
                raise LlmUnavailableError("frame role 必须是 JSON 对象")
            value = raw_value.get("value")
            if not isinstance(value, (str, int, bool)) or isinstance(value, float):
                raise LlmUnavailableError("frame role value 非法")
            role = raw_value.get("role")
            evidence = raw_value.get("evidence")
            if not isinstance(role, str) or not isinstance(evidence, str):
                raise LlmUnavailableError("frame role 缺少 role 或 evidence")
            roles[str(name)] = SemanticValue(value=value, role=role, evidence=evidence)  # type: ignore[arg-type]
        domain = item.get("domain")
        operation = item.get("operation")
        mode = item.get("mode")
        evidence = item.get("evidence")
        if not all(isinstance(value, str) for value in (domain, operation, mode, evidence)):
            raise LlmUnavailableError("frame 缺少 domain、operation、mode 或 evidence")
        try:
            frame = validate_frame(
                normalize_frame(
                    SemanticFrame(
                        domain=domain,  # type: ignore[arg-type]
                        operation=cast(str, operation),
                        mode=mode,  # type: ignore[arg-type]
                        evidence=cast(str, evidence),
                        confidence=_confidence(item.get("confidence", 0.0)),
                        roles=roles,
                    )
                ),
                utterance,
            )
        except FrameError as error:
            raise LlmUnavailableError(str(error)) from error
        action = action_for_frame(frame)
        if action is None or action not in allow_actions:
            continue
        frames.append(frame)
        intents.append(
            Intent(
                action=action,
                slots={"span": frame.evidence},
                confidence=frame.confidence,
                evidence=frame.evidence,
            )
        )
    return IntentProposal(
        intents=tuple(intents),
        raw=utterance,
        channel="llm-strict",
        frames=tuple(frames),
    )


def _confidence(value: object) -> float:
    try:
        confidence = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise LlmUnavailableError("confidence 非法（format_error）") from error
    if not 0.0 <= confidence <= 1.0:
        raise LlmUnavailableError("confidence 越界（format_error）")
    return confidence


class ArbitratedIntentModel:
    """双通道仲裁：规则高置信优先，LLM 只补**没被规则覆盖**的片段。

    规则通道负责已冻结的高精度口语（取消订阅、余额、账单等），LLM 负责长尾表达与上下文；
    同一片段出现两个动作时**保留规则结论**，不静默改写成另一动作——冲突会体现为
    「其余片段照常装配 + 该片段按规则执行」，仍要经过计划卡与裁决，不直接执行。
    """

    def __init__(
        self,
        primary: IntentModel,
        rules: IntentModel | None = None,
    ) -> None:
        self._primary = primary
        self._rules = rules if rules is not None else TemplateIntentModel()
        self._degraded = False

    @property
    def degraded(self) -> bool:
        """最近一次调用是否发生降级（LLM 不可用，只剩规则通道）。"""
        return self._degraded

    def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
        rule = self._rules.propose(utterance, context)
        try:
            llm = self._primary.propose(utterance, context)
        except LlmUnavailableError:
            self._degraded = True
            return IntentProposal(rule.intents, rule.raw, channel="template_fallback")
        self._degraded = False
        covered = [
            str(intent.slots.get("span", "")) for intent in rule.intents if intent.slots.get("span")
        ]
        merged: list[Intent] = list(rule.intents)
        for intent in llm.intents:
            span = str(intent.slots.get("span", ""))
            if span and any(_spans_overlap(span, other) for other in covered):
                continue
            if any(
                item.action == intent.action and str(item.slots.get("span", "")) == span
                for item in merged
            ):
                continue
            merged.append(intent)
        if not merged:
            merged = list(llm.intents)
        return IntentProposal(
            tuple(merged),
            llm.raw or rule.raw,
            channel="llm+rules",
            frames=llm.frames,
        )


def _spans_overlap(left: str, right: str) -> bool:
    """两个原文片段是否互相覆盖（含完全相同）：用于把 LLM 补位限制在未覆盖片段。"""
    return bool(left and right and (left in right or right in left))


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
