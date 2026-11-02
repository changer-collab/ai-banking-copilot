"""槽位补全：把意图的**控制槽**填成可信值；填不满就**追问**，绝不猜、不定级。

取值优先级（两条来源**都必须是可信的**，H1/H3）：
1. `selected`：受信任调用方（Web / IM 层）显式选定的对象 id；
2. 确定性解析器：`resolve_payees` / `parse_amount` / `parse_temporal`（01，零模型参与）；
3. 都不行 → 产出 `SlotQuestion`（含候选标签）。

三条铁律：
- **unverified 永不进控制槽**：取值为 `Trust.UNVERIFIED` 一律丢弃并转追问；
- **多候选收款人强制澄清**：候选 ≥ 2 时**不产出级别**、不自动择一（PRD §5.2）；
- **写入即类型把关**：每个控制槽取值都经 `contracts.taint.Slot(CONTROL, Value(TRUSTED))` 构造，
  用类型系统而非纪律保证"控制槽只见 trusted"。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

from ai_banking_copilot.contracts.taint import Slot, SlotKind, Trust, Value
from ai_banking_copilot.gateway.models import Card, Money, Payee
from ai_banking_copilot.gateway.resolvers import (
    parse_amount,
    parse_temporal,
    resolve_payees,
)
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.tools.registry import ToolSpec

_SPECS = build_default_registry()


def _slots_of(spec: ToolSpec, *, required: bool) -> tuple[str, ...]:
    """取该动作**由规划层补全**的槽位名（`ParamSpec.slot_name` 非空的那些）。

    注意判据是「是否由规划层补全」，**不是**「是否必填」：`bills.monthly` 的 `year` / `month`
    虽为必填参数，却由场景层填充，故 `slot_name` 为空、不计入槽位。

    `required` 用来分开两类槽位：**必填**缺了就追问；**可选**缺了就跳过
    （例如理财咨询的期限 / 起购上限，用户没说就按「不筛」处理）。
    """
    names: list[str] = []
    for param in spec.params:
        if param.slot_name is not None and param.required is required:
            names.append(param.slot_name)
    return tuple(names)


REQUIRED_SLOTS: Mapping[str, tuple[str, ...]] = {
    action: _slots_of(_SPECS.get(action), required=True) for action in _SPECS.actions()
}

OPTIONAL_SLOTS: Mapping[str, tuple[str, ...]] = {
    action: _slots_of(_SPECS.get(action), required=False) for action in _SPECS.actions()
}
"""动作 → **可选**控制槽（同由 02 目录派生）：缺了不追问，填了才进参数。"""

# 动作 → 必填控制槽（**由 02 目录派生**，见 `docs/已修复清单-2026-10-08.md` 步骤 1.3）。
#
# 派生而非手写：新增动作只需在场景模块声明 `ToolSpec` 的参数与 `slot_name`，本表自动跟上。
#
# **与 02 目录的控制槽一致性**：凡 02 目录声明为**必填 `user_id` 控制槽**的动作，此处必须含
# `user_id`（纯查询动作亦然：`bills.monthly` / `bills.category_trend` / `card.list` /
# `subscription.list`）；写动作（`transfer.create` / `transfer.schedule` / `wealth.subscribe` /
# `wealth.redeem` / `subscription.sign` / `card.apply_virtual`）一并覆盖。`subscription.sign`
# 的对象是**商户**（不涉及已有订阅 id），故按 `merchant` / `amount_cents` / `period` /
# `next_charge_on` 声明（README 裁决 #17）；`user_id` 由**受信任调用方**经 `selected` 填入
# （会话层与评测链装配 `DEMO_USER_ID`）。该一致性现由派生**自动成立**——两者同源于 02 目录。

def _slot_params() -> Mapping[str, str]:
    """槽位名 → 参数名（`ParamSpec.slot_name` → `ParamSpec.name`）：**由 02 目录派生**。

    冲突即抛：同一槽位名被两个动作映射到**不同**参数名 ⇒ 规划层无法判断该写哪个键，
    宁可 fail-fast，也不要静默取其一（这才是平行表真正会害人的地方）。
    """
    mapping: dict[str, str] = {}
    for action in _SPECS.actions():
        for param in _SPECS.get(action).params:
            if param.slot_name is None:
                continue
            existing = mapping.get(param.slot_name)
            if existing is not None and existing != param.name:
                raise ValueError(
                    f"槽位名 {param.slot_name!r} 被映射到多个参数名："
                    f"{existing} / {param.name}（动作 {action}）"
                )
            mapping[param.slot_name] = param.name
    return mapping


SLOT_PARAMS: Mapping[str, str] = _slot_params()
"""控制槽 → `PlanNode.params` 键名（**由 02 目录派生**，
    见 `docs/已修复清单-2026-10-08.md` 步骤 4「残留」）。

派生而非手写：新增动作只需在场景模块声明 `ParamSpec.slot_name`，本表自动跟上；两套词汇
（规划层的槽位名 / 执行层的参数名）若出现矛盾则构造期即抛，不再静默错位。"""

_PROMPTS: Mapping[str, str] = {
    "account": "请选择要操作的账户。",
    "payee": "请确认收款人（需要全名 / 账号尾号 / 手机尾号）。",
    "payer": "请确认付款人（需要全名 / 账号尾号 / 手机尾号）。",
    "amount": "请给出金额（单位：元，例如 500）。",
    "when": "请给出执行时间（例如 明天 / 2026-12-25）。",
    "product": "请选择理财产品。",
    "subscription": "请选择要处理的订阅。",
    "card": "请选择要处理的卡片（一次一张）。",
    "daily_limit": "请给出额度（必须带单位，例如 2 万元）。",
    "lock_days": "临时冻结几天？可选 5 / 7 / 10 / 15 天（默认 7 天，到期自动解挂）。",
    "loss_type": "是临时冻结（可解、5–15 天、免费），还是正式挂失（不可逆、可能收费）？",
    "lock_reason": "暂时冻结的原因是什么？（找不到卡 / 疑似风险 / 其他）",
    "loss_reason": "卡片是遗失、被盗，还是其他情况？",
    "replace": "是否同时补卡？补卡会换新卡号，旧卡作废。",
    "mail_method": "新卡怎么寄？普通邮寄（3–5 个工作日）还是加急（1–2 个工作日）。",
    "address": "新卡寄到哪个地址？（默认地址可以在卡片页弹窗里修改）",
    "limit_kind": "调整借记卡哪一项限额：ATM 取现 / 转账 / 消费 / 境外？",
    "credit_mode": "是临时额度（1 个月自动恢复），还是固定额度（需审批 3–5 个工作日）？",
    "duration_months": "临时额度生效几个月？（默认 1 个月）",
    "hold": "请选择要解冻的冻结记录。",
    "user_id": "请确认要操作的用户身份（由受信任调用方提供）。",
    "merchant": "请给出商户名（例如 鲜花礼品店 / 视频会员）。",
    "amount_cents": "请给出代扣金额（数字默认单位为分，也可输入 25 元）。",
    "period": "请给出代扣周期（每月 / 每年，或 monthly / yearly）。",
    "next_charge_on": "请给出下次扣款日（例如 2026-11-01）。",
    "horizon": "请给出资金闲置的时间（例如 半年 / 6 个月 / 90 天）。",
    "max_amount": "请给出打算投入的金额（例如 5 万元）。",
    "shares": "请给出赎回份额（份）。",
    "transfer_method": "请选择到账方式：实时 / 普通 / 次日 / 大额。",
    "purpose": "这笔钱用于什么？（房租 / 工资 / 报销 / 其他）",
}

_PERIOD_ALIASES = {"monthly": "monthly", "yearly": "yearly", "每月": "monthly", "每年": "yearly"}

_ACCOUNT_QUERIES = frozenset({"account.balance", "account.transactions"})
_QUERY_NOUN = re.compile(r"余额(?!宝)|(?:异常|可疑|大额)?交易(?:流水|记录|明细)?|流水")
_QUERY_PREFIX = re.compile(
    r"^(?:(?:请|麻烦|劳驾|帮我|帮忙|给我|我想|我要|想知道|想|把|能否|能不能|可以|先|再|就|有没有)"
    r"|(?:我)?(?:能|可以)|(?:按|照)(?:我)?选(?:择|定)?的"
    r"|(?:查询|查看|查查|查|看看|看|核对)(?:一下|下)?"
    # 「列出 / 统计 / 筛选」同样是查询动词：不剥掉会让后面的对象限定整句失配
    r"|(?:列出|列举|列|统计|汇总|筛选|找出|盘点|列一下)(?:一下|下)?|\s)+"
)
_SELF_QUERY_OBJECT = re.compile(
    r"(?:(?:我|本人|自己|名下|当前|已选|选中|这个|那个|这张|那张|该|本|在|的|银行|账户|帐户|银行卡"
    r"|储蓄卡|卡|储蓄|活期|主|可用|剩余|里|中|上|现在|目前|最近|今年|去年|本月|上月|今天|昨天"
    r"|(?:近|过去)?[一二三四五六七八九十\d]+(?:个月|年|月|天|日|周))\s*)*"
)
_THIRD_QUERY_OBJECT = re.compile(
    r"(?:他|她|别人|其他人|老婆|妻子|爱人|老公|丈夫|爸爸|妈妈|父母|儿子|女儿|孩子|家人|同事|朋友)"
    r"(?:们)?(?:的|名下|(?:那张|这张)?(?:账户|帐户|银行卡|储蓄卡|卡))"
)
_QUERY_OBJECT_PART = re.compile(r"[，,。；;、]|" + _QUERY_NOUN.pattern)
_QUERY_OBJECT_INTRO = re.compile(r"^(?:是|要|用|选)(?:查|看)?")
_QUERY_OBJECT_SPECIFIED = re.compile(r"^(?:我要|我想用|我想选|要|用|选|是(?!多少|几|什么|否))")
_QUERY_AMOUNT_FILTER = re.compile(
    r"(?:金额|数额|总计|合计)?\s*(?:超过|多于|大于|高于|不低于|不足|低于|少于|小于|不高于)\s*"
    r"[0-9一二三四五六七八九十百千万两.]+\s*(?:元|块)"
)
"""金额条件（「超过两百元」）：账户流水查询**不支持**按金额筛选，需如实说明能力边界。"""
_QUERY_PREDICATE_TAIL = re.compile(
    r"(?:本月|上月|这个月|上个月|今年|去年|近|过去)?"
    r"(?:[一二三四五六七八九十\d]+(?:个月|年|月|天|日|周))?"
    r"(?:的)?"
    r"(?:金额|数额|总计|合计)?"
    r"(?:超过|多于|大于|高于|不低于|不足|低于|少于|小于|不高于)"
    r"\s*[0-9一二三四五六七八九十百千万两.]+\s*(?:元|块)?(?:钱)?"
    r"的?(?:消费|支出|交易|记录|流水|明细)?"
)
"""查询谓词尾巴（「本月金额超过两百元的消费」）：对象限定只取谓词之前的部分。"""


@dataclass(frozen=True)
class SlotQuestion:
    """一次澄清请求（尚未成型的控制槽）。"""

    slot: str
    prompt: str
    candidates: tuple[str, ...] = ()
    choices: tuple[tuple[str, str], ...] = ()
    """`(标签, 回填值)` 可点选项：赎回只能选**本人持仓**，标签要带持有金额与到期日。"""


@dataclass(frozen=True)
class SlotFill:
    """一个意图的补全结果：已填的可信控制槽 + 待澄清项 + 解析轨迹。"""

    slots: Mapping[str, object] = field(default_factory=dict)
    questions: tuple[SlotQuestion, ...] = ()
    trace: tuple[str, ...] = ()
    negated: bool = False

    @property
    def complete(self) -> bool:
        """是否已补齐全部必填控制槽，且未被否定词拦截。"""
        return not self.questions and not self.negated


class _ProductView(Protocol):
    """`_resolve_product` 的入参形状：网关 `WealthProduct` 满足（不为依赖 gateway 而耦合）。"""

    @property
    def product_id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def risk_level(self) -> str: ...


class _HoldView(Protocol):
    """解冻候选的入参形状：网关 `Hold` 满足（只读候选，不在此层做裁决）。"""

    @property
    def hold_id(self) -> str: ...

    @property
    def amount(self) -> Money: ...

    @property
    def reason(self) -> str: ...


class _HoldingView(Protocol):
    """持仓候选的入参形状：网关 `WealthHolding` 满足（**赎回只能从持仓里选**）。"""

    @property
    def product_id(self) -> str: ...

    @property
    def amount(self) -> Money: ...

    @property
    def purchased_on(self) -> date: ...


@dataclass(frozen=True)
class SlotInputs:
    """补全输入：文本 + 受信任上下文（日期 / 名册 / 已选对象 / 产品目录）。"""

    text: str
    today: date
    payees: Sequence[Payee] = ()
    cards: Sequence[Card] = ()
    """本人卡片名册（只读）：仅用于把**卡类型词**（信用卡 / 借记卡）解析成唯一卡 id。"""
    selected: Mapping[str, object] = field(default_factory=dict)
    scope_text: str | None = None
    """当前意图的完整原文子句；模型截短的 text 不能消除对象限定。"""
    products: Sequence[_ProductView] = ()
    """受信任产品目录（`gateway.wealth_products()`）；只用于**原文匹配**，不新增实体。"""
    holdings: Sequence[_HoldingView] = ()
    """本人持仓（只读）：**赎回对象只能从这里选**，在售目录里的产品用户并未持有。"""
    holdings_known: bool = False
    """调用方是否已提供完整持仓视图；未提供时保留旧的产品目录匹配口径。"""
    default_product: str | None = None
    """兜底产品（仅申购）：原文没点名 / 没给等级时才用，且必须记 `default product:<id>`。"""
    holds: Sequence[_HoldView] = ()
    """本人当前有效冻结（只读）：用于把「取消冻结」从自由文本变成可选列表。"""


_BARE_AMOUNT_ACTIONS = frozenset(
    {
        "transfer.create", "transfer.schedule", "transfer.book",
        "wealth.subscribe", "wealth.redeem",
        # 冻结是「先冻 3000」这类口语的高频写法；裸数字仍受日期 / 序数 / 5 位以上限制，
        # 不会被当成手机号或日期。方向风险由「冻结」这个动作本身决定，不改写金额符号。
        "funds.freeze",
    }
)
"""允许**自由文本里出现无歧义裸数字**当金额的动作：转账族（「转给李娜 500」）与
理财申购 / 赎回（「赎回 500」），以及口语化的「先冻 3000」。

其余动作（额度 / 代扣签约）保持「必须带单位」：这些金额一旦读错方向更坏。
理财侧额外遮蔽 `R1`–`R5` 风险等级词，避免把等级里的数字读成金额。
"""

_SELECTED_BARE_AMOUNT_ACTIONS = frozenset(
    {"transfer.create", "transfer.schedule", "transfer.book",
     "wealth.subscribe", "wealth.redeem"}
)
"""`selected` 通道（页面下拉 / 输入框）里允许裸数字按「元」读的动作。

**与 `_BARE_AMOUNT_ACTIONS` 的差别只在冻结**：口语「先冻 3000」要能读懂，但冻结金额
一旦在表单里只填了「500」，单凭数字无法区分元 / 分，仍按「必须带单位」追问。
"""

_RISK_LEVEL_TOKEN = re.compile(r"(?<![A-Za-z0-9])R\s*([1-5])(?![0-9])", re.IGNORECASE)
"""风险等级词（`R2` / `r 3`）：既是产品选择依据，也不能被金额解析器当成数字。"""


def fill_control_slots(action: str, inputs: SlotInputs) -> SlotFill:
    """按 `REQUIRED_SLOTS[action]` 逐个补槽；返回已填槽与待澄清项。

    `negation=True` 只可能来自真正的拒绝表达（"不要 / 别 / 拒绝"等）：01 的 `parse_temporal`
    已按 README 裁决 §11 把「取消」移出否定词表，故「取消订阅」这类合法意图不会被拦截。
    """
    parsed = parse_temporal(inputs.text, inputs.today)
    if parsed.negation:
        return SlotFill(trace=(f"negation detected:{parsed.evidence}",), negated=True)

    scope_question = _query_scope_question(action, inputs)
    if scope_question is not None:
        return SlotFill(questions=(scope_question,), trace=("unresolved account query scope",))

    filled: dict[str, object] = {}
    questions: list[SlotQuestion] = []
    trace: list[str] = []
    bare_amount = action in _BARE_AMOUNT_ACTIONS
    bare_selected = action in _SELECTED_BARE_AMOUNT_ACTIONS
    for slot in REQUIRED_SLOTS.get(action, ()):
        chosen, question, notes = _resolve_slot(
            slot, inputs, action=action, bare_amount=bare_amount, bare_selected=bare_selected
        )
        trace.extend(notes)
        if chosen is not None:
            filled[slot] = _accept(slot, chosen)
        elif question is not None:
            questions.append(question)
    # 可选槽：解析得到就带上，解析不到就静默跳过（绝不因此追问）
    for slot in OPTIONAL_SLOTS.get(action, ()):
        chosen, _, notes = _resolve_slot(
            slot, inputs, action=action, bare_amount=bare_amount, bare_selected=bare_selected
        )
        trace.extend(notes)
        if chosen is not None:
            filled[slot] = _accept(slot, chosen)
    _default_full_redemption(action, inputs, filled, questions, trace)
    return SlotFill(slots=filled, questions=tuple(questions), trace=tuple(trace))


_PARTIAL_REDEEM = re.compile(r"一半|部分|只赎|先赎")
"""用户明确要**部分赎回**：这时金额必须由用户给，不能默认全部。"""


def _default_full_redemption(
    action: str,
    inputs: SlotInputs,
    filled: dict[str, object],
    questions: list[SlotQuestion],
    trace: list[str],
) -> None:
    """赎回没给金额 ⇒ 默认**全部赎回**，金额取持仓事实（用户不必再算「赎多少」）。

    真实业务按份额赎回、金额由净值事后算出；本仓库持仓只有金额，故默认全额赎回是
    与事实口径一致的做法，**不替用户编一个部分金额**。
    """
    if action != "wealth.redeem" or not inputs.holdings_known:
        return
    if "amount" not in filled and not _PARTIAL_REDEEM.search(inputs.text):
        # 没给金额就是「全部赎回」：不先问金额，等持仓定下来直接取事实金额。
        questions[:] = [question for question in questions if question.slot != "amount"]
    product_id = filled.get("product")
    if not isinstance(product_id, str) or "amount" in filled:
        return
    if _PARTIAL_REDEEM.search(inputs.text):
        return
    holding = next(
        (item for item in inputs.holdings if item.product_id == product_id), None,
    )
    if holding is None:
        return
    filled["amount"] = _accept("amount", holding.amount.cents)
    trace.append(f"full redeem of holding:{product_id} (amount from holding)")


def _query_scope_question(action: str, inputs: SlotInputs) -> SlotQuestion | None:
    """只核对账户查询的对象限定；可信账户 ID 不代表原文对象就是本人。

    这里只支持本人或账户泛称，不为命名银行、产品或第三人生成映射。
    查询名词前或明确指定的对象须可解释，不能因省略“的/账户”而套用本人账户。
    但**谓词不是对象**：「列出活期账户本月金额超过两百元的消费」里，对象只有「活期账户」，
    时间与金额条件由 `_self_subject` 先剥掉，不能因此判成他人账户。
    """
    if action not in _ACCOUNT_QUERIES:
        return None
    text = inputs.scope_text if inputs.scope_text is not None else inputs.text
    text = text.partition("备注")[0]
    outside = _THIRD_QUERY_OBJECT.search(text) is not None or any(
        re.search(re.escape(payee.full_name) + r"(?:的|名下|账户|帐户|银行卡|储蓄卡|卡)", text)
        is not None
        for payee in inputs.payees
    )
    query_noun = _QUERY_NOUN.search(text)
    if query_noun is not None:
        leading = re.split(r"[，,。；;、]", text[:query_noun.start()])[-1]
        subject = _self_subject(leading)
        if subject and not _SELF_QUERY_OBJECT.fullmatch(subject):
            outside = True
    # 对象可能跟在查询词后，也可能没有“余额”等查询名词；完整 scope 均需保留。
    for index, part in enumerate(_QUERY_OBJECT_PART.split(text)):
        subject = _QUERY_PREFIX.sub("", part).strip()
        introduced = _QUERY_OBJECT_INTRO.match(subject) is not None
        specified = (
            _QUERY_OBJECT_SPECIFIED.match(part.strip()) is not None
            or _QUERY_OBJECT_SPECIFIED.match(subject) is not None
        )
        subject = _QUERY_OBJECT_INTRO.sub("", subject).strip()
        explicit_subject = any(
            token in subject for token in ("银行", "账户", "帐户", "银行卡", "储蓄卡", "余额宝")
        ) or ((index == 0 or introduced) and "的" in subject) or (specified and bool(subject))
        # 判定只保留对象本身：「本月金额超过两百元的消费」这类谓词不是对象的一部分。
        if explicit_subject and not _SELF_QUERY_OBJECT.fullmatch(_self_subject(subject)):
            outside = True
            break
    if not outside:
        return None
    return _outside_scope_question()


def _outside_scope_question() -> SlotQuestion:
    return SlotQuestion(
        "account",
        "当前仅支持查询所列本人演示账户，原文对象尚未匹配；请明确改查本人账户后重新提交。",
    )


def _self_subject(text: str) -> str:
    """剥掉动词前缀、引语、金额条件与时间谓词，只留对象限定本身。"""
    subject = _QUERY_PREFIX.sub("", text).strip()
    subject = _QUERY_OBJECT_INTRO.sub("", subject).strip()
    subject = _QUERY_PREDICATE_TAIL.sub("", subject).strip()
    subject = _QUERY_AMOUNT_FILTER.sub("", subject).strip()
    return re.sub(r"(?:的|里|中)$", "", subject).strip()


def _accept(slot: str, value: object) -> object:
    """把可信值写入控制槽：经 `contracts.taint.Slot` 构造做**类型层**把关。"""
    Slot(kind=SlotKind.CONTROL, value=Value(value, Trust.TRUSTED))
    return value


def _resolve_slot(
    slot: str, inputs: SlotInputs, *, action: str = "", bare_amount: bool = False,
    bare_selected: bool = False,
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    notes: list[str] = []
    # 卡类型词是原文明说的对象限定，优先于默认 / 页面选中的卡：
    # 否则「申请信用卡临时额度」会被默认借记卡顶替，误判为「只有信用卡支持…」。
    if slot == "card" and inputs.cards and sum(
        1 for _, pattern in _CARD_KIND_ALIASES if pattern.search(inputs.text)
    ) == 1:
        return _resolve_card(inputs, notes)
    # 页面把未选的下拉框以 `null` 提交：显式 null 与「不传」同口径，落到解析器 / 追问，
    # 不能因为多了一个空键就改口径（见 2026-10-27 验收记录 §5 缺陷 8）。
    if inputs.selected.get(slot) is not None:
        raw = inputs.selected[slot]
        if _is_trusted(raw):
            notes.append(f"selected:{slot}")
            if slot in {"payee", "payer"}:
                chosen, question, payee_notes = _resolve_selected_payee(slot, raw, inputs)
                notes.extend(payee_notes)
                if chosen is not None:
                    return chosen, None, tuple(notes)
                return None, question, tuple(notes)
            if slot == "hold":
                chosen, question, hold_notes = _resolve_selected_hold(raw, inputs)
                notes.extend(hold_notes)
                if chosen is not None:
                    return chosen, None, tuple(notes)
                return None, question, tuple(notes)
            value = _normalize_selected(
                slot,
                raw.raw if isinstance(raw, Value) else raw,
                inputs.today,
                bare_amount=bare_selected,
            )
            if value is not None:
                return value, None, tuple(notes)
            notes.append(f"invalid selected:{slot}")
            return None, SlotQuestion(slot, _PROMPTS[slot]), tuple(notes)
        notes.append(f"rejected unverified:{slot}")
    if slot in {"payee", "payer"}:
        return _resolve_payee(inputs, notes)
    if slot == "hold":
        return _resolve_hold(inputs, notes)
    if slot in {"amount", "amount_cents"}:
        return _resolve_amount(inputs, notes, slot, bare_amount=bare_amount)
    if slot == "shares":
        return _resolve_shares(inputs, notes)
    if slot == "product":
        return _resolve_product(inputs, notes, action=action)
    if slot == "daily_limit":
        return _resolve_daily_limit(inputs, notes)
    if slot == "card":
        return _resolve_card(inputs, notes)
    if slot == "when":
        # 「24 小时延时 / 次日到账」没给具体日期：落到明天（登记不扣款，可撤销）。
        if action in {"transfer.book", "transfer.schedule"} and re.search(
            r"24\s*小时|二十四小时|次日|延时|延迟", inputs.text
        ):
            return (inputs.today + timedelta(days=1)).isoformat(), None, tuple(notes)
        return _resolve_when(inputs, notes)
    if slot == "horizon":
        return _resolve_horizon(inputs, notes)
    if slot == "max_amount":
        return _resolve_amount(inputs, notes, slot, bare_amount=False)
    if slot == "period":
        periods = {
            canonical
            for alias, canonical in _PERIOD_ALIASES.items()
            if re.search(rf"\b{alias}\b" if alias.isascii() else alias, inputs.text)
        }
        if len(periods) == 1:
            return periods.pop(), None, tuple(notes)
    if slot == "transfer_method":
        text = inputs.text
        if re.search(r"次日|明天到账|24\s*小时|二十四小时|延时|延迟", text):
            return "next_day", None, tuple(notes)
        if re.search(r"普通|2\s*小时|两小时", text):
            return "normal", None, tuple(notes)
        if re.search(r"实时|秒到", text):
            return "realtime", None, tuple(notes)
        if re.search(r"大额", text):
            return "large", None, tuple(notes)
        # 没提方式就按动作给默认值：登记未来转账默认次日，其余默认实时。
        return ("next_day" if action in {"transfer.book", "transfer.schedule"} else "realtime",
                None, tuple(notes))
    if slot == "purpose":
        labelled = re.search(
            r"(?:用途|用于|用来)(?:是|为)?\s*[:：]?\s*(房租|工资|报销|其他)", inputs.text
        )
        if labelled is not None:
            return _CHOICE_ALIASES["purpose"][labelled.group(1)], None, tuple(notes)
        for alias, canonical in _CHOICE_ALIASES["purpose"].items():
            if alias in inputs.text:
                return canonical, None, tuple(notes)
        return None, None, tuple(notes)
    if slot == "merchant":
        # 只取明确的「给某商户开通」原文，商户缺失或修改指令仍需澄清。
        match = re.search(r"给(?P<merchant>[^，,。；;]{1,30}?)开通", inputs.text)
        if match is not None:
            return match["merchant"].strip(), None, tuple(notes)
        # 显式标签（「商户 鲜花礼品店」/「商户名称：X」）：用户在补参时最常这么写。
        # 只认带分隔符的写法，避免把「商户名称是什么」这类问句读成商户。
        labelled = re.search(
            r"(?:商户名称|商户名|商户|商家)(?:\s*[:：]\s*|\s+)"
            r"(?P<merchant>[^，,。；;\s]{2,20})",
            inputs.text,
        )
        if labelled is not None:
            return labelled["merchant"].strip(" ·・"), None, tuple(notes)
    if slot == "loss_type":
        if re.search(r"临时|口头|先冻|暂时", inputs.text):
            return "temporary", None, tuple(notes)
        if re.search(r"正式|永久|作废|彻底", inputs.text):
            return "formal", None, tuple(notes)
    if slot == "limit_kind":
        channels = (
            ("atm", r"atm|取现|现金"),
            ("transfer", r"转账|转出"),
            ("purchase", r"消费|刷卡|pos"),
            ("overseas", r"境外|海外|出国"),
        )
        matched = [key for key, pattern in channels if re.search(pattern, inputs.text, re.I)]
        if len(matched) == 1:
            return matched[0], None, tuple(notes)
    if slot == "credit_mode":
        if re.search(r"临时|短期", inputs.text):
            return "temporary", None, tuple(notes)
        if re.search(r"固定|永久|长期|提升|提高", inputs.text):
            return "permanent", None, tuple(notes)
    if slot == "mail_method":
        if re.search(r"加急|快递|顺丰|1.{0,2}2\s*天", inputs.text):
            return "express", None, tuple(notes)
        if re.search(r"普通|平邮|挂号|3.{0,2}5\s*天", inputs.text):
            return "standard", None, tuple(notes)
    if slot == "replace":
        if re.search(r"不补|只挂失|无需补|不要补", inputs.text):
            return "no", None, tuple(notes)
        if re.search(r"补卡|一起补|同时补|换卡", inputs.text):
            return "yes", None, tuple(notes)
    if slot == "address":
        labelled = re.search(
            r"(?:地址|寄到|送到)(?:\s*[:：]\s*|\s*)(?P<address>[^，,。；;\n]{5,80})",
            inputs.text,
        )
        if labelled is not None:
            return labelled["address"].strip(), None, tuple(notes)
    if slot == "next_charge_on":
        match = re.search(r"下个?月\s*(?P<day>\d{1,2})\s*[号日]", inputs.text)
        if match is not None:
            next_month = inputs.today.month % 12 + 1
            year = inputs.today.year + (inputs.today.month == 12)
            try:
                return date(year, next_month, int(match["day"])).isoformat(), None, tuple(notes)
            except ValueError:
                return None, SlotQuestion(slot, _PROMPTS[slot]), tuple(notes)
        parsed = parse_temporal(inputs.text, inputs.today)
        if parsed.matched and parsed.when is not None:
            return parsed.when.isoformat(), None, tuple(notes)
    return None, SlotQuestion(slot=slot, prompt=_PROMPTS[slot]), tuple(notes)


_CHOICE_ALIASES: Mapping[str, Mapping[str, str]] = {
    "loss_type": {
        "temporary": "temporary", "临时": "temporary", "临时冻结": "temporary",
        "口头挂失": "temporary", "formal": "formal", "正式": "formal",
        "正式挂失": "formal", "永久": "formal",
    },
    "limit_kind": {
        "atm": "atm", "取现": "atm", "cash": "atm",
        "transfer": "transfer", "转账": "transfer",
        "purchase": "purchase", "消费": "purchase", "刷卡": "purchase",
        "overseas": "overseas", "境外": "overseas", "海外": "overseas",
    },
    "credit_mode": {
        "temporary": "temporary", "临时": "temporary",
        "permanent": "permanent", "固定": "permanent", "永久": "permanent",
    },
    "mail_method": {
        "standard": "standard", "普通": "standard",
        "express": "express", "加急": "express",
    },
    "replace": {"yes": "yes", "是": "yes", "no": "no", "否": "no"},
    "transfer_method": {
        "realtime": "realtime", "实时": "realtime", "实时到账": "realtime", "秒到": "realtime",
        "normal": "normal", "普通": "normal", "普通到账": "normal",
        "next_day": "next_day", "次日": "next_day", "次日到账": "next_day",
        "延时": "next_day", "延迟": "next_day", "24小时": "next_day",
        "large": "large", "大额": "large", "大额通道": "large",
    },
    "purpose": {
        "rent": "rent", "房租": "rent",
        "salary": "salary", "工资": "salary",
        "reimburse": "reimburse", "报销": "reimburse",
        "other": "other", "其他": "other",
    },
}


def _normalize_selected(
    slot: str, raw: object, today: date, *, bare_amount: bool = False
) -> object | None:
    """把澄清表单的可信字符串转回工具要求的金额 / 周期 / 日期 / 枚举类型。"""
    if isinstance(raw, str) and slot in _CHOICE_ALIASES:
        return _CHOICE_ALIASES[slot].get(raw.strip())
    if isinstance(raw, str) and slot in {"lock_days", "duration_months"}:
        digits = re.search(r"\d+", raw)
        return int(digits.group()) if digits is not None else None
    if slot in {"amount", "amount_cents", "daily_limit"} and isinstance(raw, str):
        text = raw.strip()
        if slot == "amount_cents" and text.isdigit():
            return int(text)
        # 澄清表单里答一个「200」就是 200 元（与 `_resolve_amount` 同口径）；额度槽仍要求带单位。
        parsed_amount = parse_amount(text, bare_amounts=bare_amount and slot == "amount")
        return parsed_amount.value.cents if parsed_amount.value is not None else None
    if slot == "period" and isinstance(raw, str):
        return _PERIOD_ALIASES.get(raw.strip().lower())
    if slot == "when" and isinstance(raw, str):
        # 「每月几号」的补充答案（5 / 5 号 / 每月 5 号）：换算成下一次该日期的记账日。
        match = _MONTHLY_DAY_ANSWER.fullmatch(raw.strip())
        if match is not None:
            target = _next_monthly_day(int(match["day"]), today)
            return None if target is None else target.isoformat()
    if slot == "next_charge_on":
        if isinstance(raw, date):
            return raw.isoformat()
        if isinstance(raw, str):
            try:
                return date.fromisoformat(raw.strip()).isoformat()
            except ValueError:
                parsed_date = parse_temporal(raw, today)
                if parsed_date.matched and parsed_date.when is not None:
                    return parsed_date.when.isoformat()
        return None
    return raw


_MONTHLY_DAY_ANSWER = re.compile(r"^(?:每(?:个)?月)?\s*(?P<day>\d{1,2})\s*[号日]?$")
"""澄清答案里的「每月几号」（`5` / `5 号` / `每月 5 号`）：只在 when 槽被显式回答时生效。"""


def _next_monthly_day(day: int, today: date) -> date | None:
    """下一次「每月第 day 天」；当天命中当天。非法日（0 / 32）返回 `None`，不夹到月末。"""
    if not 1 <= day <= 31:
        return None
    year, month = today.year, today.month
    for _ in range(13):
        try:
            candidate = date(year, month, day)
        except ValueError:
            candidate = None
        if candidate is not None and candidate >= today:
            return candidate
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return None


def _is_trusted(value: object) -> bool:
    if isinstance(value, Value):
        return value.trust is Trust.TRUSTED
    return True


def _resolve_payee(
    inputs: SlotInputs, notes: list[str]
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    resolution = resolve_payees(inputs.payees, inputs.text)
    if resolution.needs_clarification:
        labels = tuple(_payee_label(item) for item in resolution.candidates)
        notes.append(f"clarify payee:{','.join(labels)}")
        return None, SlotQuestion("payee", _PROMPTS["payee"], labels), tuple(notes)
    if not resolution.candidates:
        notes.append("missing payee")
        return None, SlotQuestion("payee", _PROMPTS["payee"]), tuple(notes)
    chosen = resolution.candidates[0]
    notes.append(f"resolved payee:{chosen.payee_id}")
    return chosen.payee_id, None, tuple(notes)


def _resolve_selected_payee(
    slot: str, raw: object, inputs: SlotInputs,
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    """澄清表单里的收款人：**先认 payee_id，再按名册解析人名**。

    之前把自由文本直接当 `payee_id` 透传，导致「李娜」被判成「所选对象不存在」；
    名册里没有（例如「妈妈」）时不再直接拒绝，而是列出本人名册让用户点选。
    """
    text = raw.raw if isinstance(raw, Value) else raw
    text = str(text).strip()
    notes: list[str] = []
    if any(payee.payee_id == text for payee in inputs.payees):
        notes.append(f"selected payee id:{text}")
        return text, None, tuple(notes)
    resolution = resolve_payees(inputs.payees, text)
    if resolution.needs_clarification:
        labels = tuple(_payee_label(item) for item in resolution.candidates)
        notes.append("clarify selected payee")
        return None, SlotQuestion(slot, _PROMPTS[slot], labels), tuple(notes)
    if resolution.candidates:
        chosen = resolution.candidates[0]
        notes.append(f"resolved selected payee:{chosen.payee_id}")
        return chosen.payee_id, None, tuple(notes)
    labels = tuple(_payee_label(payee) for payee in inputs.payees)
    notes.append("unmatched selected payee")
    prompt = f"名册里没有「{text}」，请选择要转给谁。"
    return None, SlotQuestion(slot, prompt, labels), tuple(notes)


def _hold_label(hold: _HoldView) -> str:
    return f"冻结 {hold.amount.yuan} 元 · {hold.reason}"


def _resolve_hold(
    inputs: SlotInputs, notes: list[str]
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    """解冻对象：只从本人当前有效冻结里选，不从自由文本猜 hold id。"""
    if not inputs.holds:
        notes.append("no active hold")
        return None, SlotQuestion("hold", "当前没有可解冻的冻结记录。"), tuple(notes)
    labels = tuple(_hold_label(item) for item in inputs.holds)
    notes.append(f"clarify hold:{len(labels)}")
    return None, SlotQuestion("hold", _PROMPTS["hold"], labels), tuple(notes)


def _resolve_selected_hold(
    raw: object, inputs: SlotInputs,
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    text = raw.raw if isinstance(raw, Value) else raw
    text = str(text).strip()
    notes: list[str] = []
    for hold in inputs.holds:
        if hold.hold_id == text:
            notes.append(f"selected hold:{text}")
            return text, None, tuple(notes)
    labels = tuple(_hold_label(item) for item in inputs.holds)
    notes.append("unmatched selected hold")
    return None, SlotQuestion("hold", _PROMPTS["hold"], labels), tuple(notes)


_CARD_KIND_ALIASES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("credit", re.compile(r"信用卡|贷记卡")),
    ("debit", re.compile(r"借记卡|储蓄卡")),
)
"""卡类型词 → `cards.card_type`。只做词面映射：库内未存卡号 / 尾号，尾号一律不猜。"""

_CARD_BULK = re.compile(r"都|全部|所有|每张|各张|两张|多张|逐一|一并")
"""批量 / 全量语义（「把可疑的都锁卡」）：一次只能处理一张卡，命中即说明做不到。"""

CARD_BULK_PROMPT = (
    "一次只能锁定一张卡：交易流水未记录卡号，无法从可疑交易反推是哪张卡。"
    "请指定要处理的卡片（借记卡 / 信用卡），或到记录页逐笔核对。"
)


def _resolve_card(
    inputs: SlotInputs, notes: list[str]
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    """卡片槽：全量语义先说明边界；卡类型词唯一命中则绑定，否则追问。

    这里的两种「不猜」都是硬约束：
    - 「都 / 全部」类请求**不挑一张顶替**——那会把批量止损伪装成已完成；
    - 类型词命中多张（同类型两张）仍追问，不按 id / 顺序取第一张。
    """
    text = inputs.text
    if _CARD_BULK.search(text):
        notes.append("card:bulk request unsupported")
        return None, SlotQuestion("card", CARD_BULK_PROMPT), tuple(notes)
    kinds = {kind for kind, pattern in _CARD_KIND_ALIASES if pattern.search(text)}
    if len(kinds) == 1:
        kind = kinds.pop()
        matches = [item for item in inputs.cards if item.card_type == kind]
        if len(matches) == 1:
            notes.append(f"resolved card:{matches[0].card_id}")
            return matches[0].card_id, None, tuple(notes)
        notes.append(f"ambiguous card kind:{kind}")
    return None, SlotQuestion("card", _PROMPTS["card"]), tuple(notes)


_CN_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
"""期限里允许出现的中文数字（够用即可，不做通用中文数字解析）。"""

_HORIZON_DAYS = re.compile(r"(\d+)\s*天")
_HORIZON_MONTHS = re.compile(r"(半|\d+|[一二两三四五六七八九十])\s*(?:个)?月")
_HORIZON_YEARS = re.compile(r"(半|\d+|[一二两三四五六七八九十])\s*年")


def _cn_number(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _CN_DIGITS.get(token)


def parse_horizon_days(text: str) -> int | None:
    """从一句话里读「资金闲置多久」（返回天数）；读不到就 `None`（**不猜**）。

    顺序：天 → 月 → 年。只认用户自己说的时间，不做「大概」推断；
    「半年」按 180 天算并如实写进回复，避免 182/183 天的假精确。
    """
    days = _HORIZON_DAYS.search(text)
    if days is not None:
        return int(days.group(1))
    months = _HORIZON_MONTHS.search(text)
    if months is not None:
        token = months.group(1)
        if token == "半":
            return 180
        value = _cn_number(token)
        return None if value is None else value * 30
    years = _HORIZON_YEARS.search(text)
    if years is not None:
        token = years.group(1)
        if token == "半":
            return 180
        value = _cn_number(token)
        return None if value is None else value * 365
    return None


def _resolve_horizon(
    inputs: SlotInputs, notes: list[str]
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    """期限槽（**可选**）：读不到就返回「无值但不追问」，交给场景层按不筛处理。"""
    days = parse_horizon_days(inputs.text)
    if days is None or days <= 0:
        notes.append("optional horizon: none")
        return None, None, tuple(notes)
    notes.append(f"resolved horizon:{days}")
    return days, None, tuple(notes)


def _resolve_amount(
    inputs: SlotInputs, notes: list[str], slot: str, *, bare_amount: bool = False
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    """金额槽：转账族 / 理财允许**无歧义的裸数字**（「转给李娜 500」「赎回 500」）。

    `R2` 这类**风险等级词**在理财句子里不是金额：裸数字放宽时先遮蔽等级词，
    再交给共享解析器（不在 `gateway/resolvers.py` 里硬编码理财词汇）。
    """
    text = _RISK_LEVEL_TOKEN.sub(" ", inputs.text) if bare_amount else inputs.text
    parsed = parse_amount(
        text,
        bare_amounts=bare_amount,
        reserved_digits=_reserved_digits(inputs.payees),
    )
    if not parsed.matched or parsed.value is None:
        notes.append(f"missing {slot}")
        return None, SlotQuestion(slot, _PROMPTS[slot]), tuple(notes)
    notes.append(f"resolved {slot}:{parsed.value.cents}")
    return parsed.value.cents, None, tuple(notes)


_SHARES = re.compile(r"(\d+(?:\.\d{1,4})?)\s*份")


def _resolve_shares(
    inputs: SlotInputs, notes: list[str]
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    """份额槽（可选）：只认「X 份」；读不到就静默跳过，绝不追问、绝不猜。"""
    match = _SHARES.search(inputs.text)
    if match is None:
        return None, None, tuple(notes)
    whole, _, fraction = match.group(1).partition(".")
    share_bp = int(whole) * 10000 + int((fraction + "0000")[:4])
    if share_bp <= 0:
        return None, None, tuple(notes)
    notes.append(f"resolved shares:{share_bp}")
    return share_bp, None, tuple(notes)


def _squash(text: str) -> str:
    """去空白后比对：目录里的「稳利债券 A」与口述「稳利债券A」是同一只。"""
    return re.sub(r"\s+", "", text)


def _resolve_product(
    inputs: SlotInputs, notes: list[str], *, action: str = ""
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    """产品槽：只做**原文匹配 + 目录兜底**，绝不凭语义猜产品。

    顺序：① 原文出现 `product_id`；② 原文出现产品全名（最长优先）；
    ③ 原文出现唯一的 `R1`–`R5` 等级且目录内该等级只有一只；④ 申购兜底产品；
    ⑤ 否则追问并列出目录（不隐藏被拒产品）。

    **赎回例外**：赎回对象必须来自本人持仓（见 `_resolve_redeem_product`），
    在售目录里的产品用户并没有持有，不能拿来「选」。
    """
    if action == "wealth.redeem" and inputs.holdings_known:
        return _resolve_redeem_product(inputs, notes)
    text = inputs.text
    lowered = text.lower()
    for product in inputs.products:
        if product.product_id and product.product_id.lower() in lowered:
            notes.append(f"resolved product:{product.product_id} (id)")
            return product.product_id, None, tuple(notes)
    squashed = _squash(text)
    named = sorted(
        (item for item in inputs.products if _squash(item.name) and _squash(item.name) in squashed),
        key=lambda item: len(_squash(item.name)),
        reverse=True,
    )
    if named:
        notes.append(f"resolved product:{named[0].product_id} (name)")
        return named[0].product_id, None, tuple(notes)
    levels = {f"R{level}" for level in _RISK_LEVEL_TOKEN.findall(text)}
    if len(levels) == 1:
        level = levels.pop()
        matched = [
            item for item in inputs.products if item.risk_level.upper() == level
        ]
        if len(matched) == 1:
            notes.append(f"resolved product:{matched[0].product_id} (risk {level})")
            return matched[0].product_id, None, tuple(notes)
        if len(matched) > 1:
            notes.append(f"clarify product:risk {level}")
            return None, _product_question(inputs, tuple(matched)), tuple(notes)
    if inputs.default_product:
        notes.append(f"default product:{inputs.default_product}")
        return inputs.default_product, None, tuple(notes)
    notes.append("missing product")
    return None, _product_question(inputs, ()), tuple(notes)


def _product_question(
    inputs: SlotInputs, candidates: Sequence[_ProductView]
) -> SlotQuestion:
    pool = candidates or inputs.products
    if not pool:
        return SlotQuestion("product", _PROMPTS["product"])
    labels = tuple(f"{item.name}（{item.risk_level}）" for item in pool)
    return SlotQuestion(
        "product",
        f"{_PROMPTS['product']}可选：" + "、".join(labels),
        labels,
    )


_MATURITY_WORDS = re.compile(r"到期|满期")


def _product_by_id(inputs: SlotInputs, product_id: str) -> _ProductView | None:
    for product in inputs.products:
        if product.product_id == product_id:
            return product
    return None


def _holding_maturity(holding: _HoldingView, inputs: SlotInputs) -> date | None:
    """持仓到期日 = 起息日 + 产品期限；期限不明（如活期）返回 `None`（不猜）。"""
    product = _product_by_id(inputs, holding.product_id)
    term = getattr(product, "term_days", 0) if product is not None else 0
    if not isinstance(term, int) or term <= 0:
        return None
    return holding.purchased_on + timedelta(days=term)


def _holding_label(holding: _HoldingView, inputs: SlotInputs) -> str:
    product = _product_by_id(inputs, holding.product_id)
    name = product.name if product is not None else holding.product_id
    maturity = _holding_maturity(holding, inputs)
    when = f"{maturity.isoformat()} 到期" if maturity is not None else "期限不明"
    return f"{name}（持有 {holding.amount.yuan} 元，{when}）"


def _redeem_question(
    inputs: SlotInputs, holdings: Sequence[_HoldingView], prompt: str | None = None
) -> SlotQuestion:
    choices = tuple((_holding_label(item, inputs), item.product_id) for item in holdings)
    return SlotQuestion(
        "product",
        prompt or "请选择要赎回的持仓（只能赎回本人持有的产品）：",
        tuple(label for label, _ in choices),
        choices,
    )


def _resolve_redeem_product(
    inputs: SlotInputs, notes: list[str]
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    """赎回对象**只从本人持仓里选**：在售目录不是可赎回清单。

    顺序：① 原文点名（产品 id / 名称）且确实持有；② 只有一笔持仓；
    ③ 原文说「到期」：点名日期能对上就取那一笔，否则取最近要到期的一笔
    （这是从持仓事实派生的缺省值，不猜产品）；④ 其余情况列**持仓**让用户选。
    """
    holdings = tuple(inputs.holdings)
    if not holdings:
        notes.append("no holdings to redeem")
        return None, SlotQuestion("product", "你当前没有可赎回的理财持仓。"), tuple(notes)

    # 「生日那天有笔理财到期，正好赎回」：到期线索在另一分句里，必须看整句；
    # 金额仍只看本意图的 span（不能把别处的数字读成赎回金额）。
    hint = inputs.text if not inputs.scope_text else f"{inputs.text} {inputs.scope_text}"
    lowered = hint.lower()
    squashed = _squash(hint)
    named = [
        holding for holding in holdings
        if holding.product_id.lower() in lowered
        or (
            (product := _product_by_id(inputs, holding.product_id)) is not None
            and _squash(product.name)
            and _squash(product.name) in squashed
        )
    ]
    if len(named) == 1:
        notes.append(f"resolved redeem holding:{named[0].product_id} (named)")
        return named[0].product_id, None, tuple(notes)
    if len(holdings) == 1:
        notes.append(f"resolved redeem holding:{holdings[0].product_id} (only holding)")
        return holdings[0].product_id, None, tuple(notes)
    if _MATURITY_WORDS.search(hint):
        dated = [
            (holding, maturity)
            for holding in holdings
            if (maturity := _holding_maturity(holding, inputs)) is not None
        ]
        parsed = parse_temporal(hint, inputs.today)
        if parsed.matched and parsed.when is not None:
            same_day = [holding for holding, maturity in dated if maturity == parsed.when]
            if len(same_day) == 1:
                notes.append(f"resolved redeem holding:{same_day[0].product_id} (maturity)")
                return same_day[0].product_id, None, tuple(notes)
            if not same_day:
                notes.append("no holding matures on the stated date")
                return None, _redeem_question(
                    inputs, holdings,
                    f"本人持仓里没有在 {parsed.when.isoformat()} 到期的产品；"
                    "请选择要赎回的持仓：",
                ), tuple(notes)
        upcoming = [item for item in dated if item[1] >= inputs.today]
        if upcoming:
            holding, maturity = min(upcoming, key=lambda item: (item[1], item[0].product_id))
            notes.append(
                f"resolved redeem holding:{holding.product_id} (nearest maturity {maturity})"
            )
            return holding.product_id, None, tuple(notes)
        if dated:
            holding, maturity = min(dated, key=lambda item: (item[1], item[0].product_id))
            notes.append(
                f"resolved redeem holding:{holding.product_id} (latest maturity {maturity})"
            )
            return holding.product_id, None, tuple(notes)
    notes.append("clarify redeem holding")
    return None, _redeem_question(inputs, holdings), tuple(notes)


def _reserved_digits(payees: Sequence[Payee]) -> frozenset[str]:
    """名册里的卡号尾号 / 手机尾号：这些数字属于收款人识别，不能被同时读成金额。"""
    return frozenset(
        digits
        for payee in payees
        for digits in (payee.account_tail, payee.phone_tail)
        if digits
    )


def _resolve_daily_limit(
    inputs: SlotInputs, notes: list[str]
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    """额度槽：只认**带单位**的文本金额（与 `amount` 同一套确定性解析；无单位不猜）。

    `selected`（受信任调用方显式选定）仍**优先**于本解析器，见 `_resolve_slot` 首段。
    落点见 `SLOT_PARAMS["daily_limit"]`（→ `daily_limit_cents`，整数分）。
    """
    parsed = parse_amount(inputs.text)
    if not parsed.matched or parsed.value is None:
        notes.append("missing daily_limit")
        return None, SlotQuestion("daily_limit", _PROMPTS["daily_limit"]), tuple(notes)
    notes.append(f"resolved daily_limit:{parsed.value.cents}")
    return parsed.value.cents, None, tuple(notes)


def _resolve_when(
    inputs: SlotInputs, notes: list[str]
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    parsed = parse_temporal(inputs.text, inputs.today)
    if parsed.matched and parsed.when is not None:
        notes.append(f"resolved when:{parsed.when.isoformat()}")
        return parsed.when.isoformat(), None, tuple(notes)
    notes.append(f"default when:{inputs.today.isoformat()}")
    return inputs.today.isoformat(), None, tuple(notes)


class _NamedParty(Protocol):
    """`_payee_label` 的入参形状：名册 `Payee` 与解析候选 `Candidate` 都满足。"""

    @property
    def full_name(self) -> str: ...

    @property
    def account_tail(self) -> str: ...

    @property
    def phone_tail(self) -> str: ...


def _payee_label(candidate: _NamedParty) -> str:
    return (
        f"{candidate.full_name}"
        f"(账号尾号 {candidate.account_tail} / 手机尾号 {candidate.phone_tail})"
    )
