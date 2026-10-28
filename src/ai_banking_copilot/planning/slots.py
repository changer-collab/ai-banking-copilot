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
from datetime import date
from typing import Protocol

from ai_banking_copilot.contracts.taint import Slot, SlotKind, Trust, Value
from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.gateway.resolvers import (
    parse_amount,
    parse_temporal,
    resolve_payees,
)
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.tools.registry import ToolSpec

_SPECS = build_default_registry()


def _slots_of(spec: ToolSpec) -> tuple[str, ...]:
    """取该动作**由规划层补全**的槽位名（`ParamSpec.slot_name` 非空的那些）。

    注意判据是「是否由规划层补全」，**不是**「是否必填」：`bills.monthly` 的 `year` / `month`
    虽为必填参数，却由场景层填充，故 `slot_name` 为空、不计入槽位。
    """
    names: list[str] = []
    for param in spec.params:
        if param.slot_name is not None:
            names.append(param.slot_name)
    return tuple(names)


REQUIRED_SLOTS: Mapping[str, tuple[str, ...]] = {
    action: _slots_of(_SPECS.get(action)) for action in _SPECS.actions()
}
"""动作 → 必填控制槽（**由 02 目录派生**，见 `docs/已修复清单-2026-10-08.md` 步骤 1.3）。

派生而非手写：新增动作只需在场景模块声明 `ToolSpec` 的参数与 `slot_name`，本表自动跟上。

**与 02 目录的控制槽一致性**：凡 02 目录声明为**必填 `user_id` 控制槽**的动作，此处必须含
`user_id`（纯查询动作亦然：`bills.monthly` / `bills.category_trend` / `card.list` /
`subscription.list`）；写动作（`transfer.create` / `transfer.schedule` / `wealth.subscribe` /
`wealth.redeem` / `subscription.sign` / `card.apply_virtual`）一并覆盖。`subscription.sign`
的对象是**商户**（不涉及已有订阅 id），故按 `merchant` / `amount_cents` / `period` /
`next_charge_on` 声明（README 裁决 #17）；`user_id` 由**受信任调用方**经 `selected` 填入
（会话层与评测链装配 `DEMO_USER_ID`）。该一致性现由派生**自动成立**——两者同源于 02 目录。
"""

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
    "amount": "请给出金额（必须带单位，例如 500 元）。",
    "when": "请给出执行时间（例如 明天 / 2026-12-25）。",
    "product": "请选择理财产品。",
    "subscription": "请选择要处理的订阅。",
    "card": "请选择要处理的卡片。",
    "daily_limit": "请给出额度（必须带单位，例如 2 万元）。",
    "hold": "请选择要解冻的冻结记录。",
    "user_id": "请确认要操作的用户身份（由受信任调用方提供）。",
    "merchant": "请给出商户名（例如 鲜花礼品店 / 视频会员）。",
    "amount_cents": "请给出代扣金额（数字默认单位为分，也可输入 25 元）。",
    "period": "请给出代扣周期（每月 / 每年，或 monthly / yearly）。",
    "next_charge_on": "请给出下次扣款日（例如 2026-11-01）。",
}

_PERIOD_ALIASES = {"monthly": "monthly", "yearly": "yearly", "每月": "monthly", "每年": "yearly"}

_ACCOUNT_QUERIES = frozenset({"account.balance", "account.transactions"})
_QUERY_NOUN = re.compile(r"余额(?!宝)|(?:异常|可疑|大额)?交易(?:流水|记录|明细)?|流水")
_QUERY_PREFIX = re.compile(
    r"^(?:(?:请|麻烦|劳驾|帮我|帮忙|给我|我想|我要|想知道|想|把|能否|能不能|可以|先|再|就|有没有)"
    r"|(?:我)?(?:能|可以)|(?:按|照)(?:我)?选(?:择|定)?的"
    r"|(?:查询|查看|查查|查|看看|看|核对)(?:一下|下)?|\s)+"
)
_SELF_QUERY_OBJECT = re.compile(
    r"(?:(?:我|本人|自己|名下|当前|已选|选中|这个|那个|这张|那张|该|本|在|的|银行|账户|帐户|银行卡"
    r"|储蓄卡|卡|储蓄|活期|主|可用|剩余|里|中|上|现在|目前|最近|今年|去年|本月|上月|今天|昨天"
    r"|(?:近|过去)?[一二三四五六七八九十\d]+(?:个月|年|月|天|日|周))\s*)*"
)
_THIRD_QUERY_OBJECT = re.compile(
    r"(?:他|她|别人|其他人)(?:们)?(?:的|名下|(?:那张|这张)?(?:账户|帐户|银行卡|储蓄卡|卡))"
)
_QUERY_OBJECT_PART = re.compile(r"[，,。；;、]|" + _QUERY_NOUN.pattern)
_QUERY_OBJECT_INTRO = re.compile(r"^(?:是|要|用|选)(?:查|看)?")
_QUERY_OBJECT_SPECIFIED = re.compile(r"^(?:我要|我想用|我想选|要|用|选|是(?!多少|几|什么|否))")


@dataclass(frozen=True)
class SlotQuestion:
    """一次澄清请求（尚未成型的控制槽）。"""

    slot: str
    prompt: str
    candidates: tuple[str, ...] = ()


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


@dataclass(frozen=True)
class SlotInputs:
    """补全输入：文本 + 受信任上下文（日期 / 名册 / 已选对象）。"""

    text: str
    today: date
    payees: Sequence[Payee] = ()
    selected: Mapping[str, object] = field(default_factory=dict)
    scope_text: str | None = None
    """当前意图的完整原文子句；模型截短的 text 不能消除对象限定。"""


_BARE_AMOUNT_ACTIONS = frozenset({"transfer.create", "transfer.schedule", "transfer.book"})
"""允许**无歧义裸数字**当金额的动作：只有转账族（「转给李娜 500」）。

其余动作（冻结 / 额度 / 代扣签约）保持「必须带单位」：这些金额一旦读错方向更坏，
且 2026-10-27 验收只批准放宽转账金额槽。
"""


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
    for slot in REQUIRED_SLOTS.get(action, ()):
        chosen, question, notes = _resolve_slot(slot, inputs, bare_amount=bare_amount)
        trace.extend(notes)
        if chosen is not None:
            filled[slot] = _accept(slot, chosen)
        elif question is not None:
            questions.append(question)
    return SlotFill(slots=filled, questions=tuple(questions), trace=tuple(trace))


def _query_scope_question(action: str, inputs: SlotInputs) -> SlotQuestion | None:
    """只核对账户查询的对象限定；可信账户 ID 不代表原文对象就是本人。

    这里只支持本人或账户泛称，不为命名银行、产品或第三人生成映射。
    查询名词前或明确指定的对象须可解释，不能因省略“的/账户”而套用本人账户。
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
        subject = _QUERY_PREFIX.sub("", leading).strip()
        subject = _QUERY_OBJECT_INTRO.sub("", subject).strip()
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
        if explicit_subject and not _SELF_QUERY_OBJECT.fullmatch(subject):
            outside = True
            break
    if not outside:
        return None
    return SlotQuestion(
        "account",
        "当前仅支持查询所列本人演示账户，原文对象尚未匹配；请明确改查本人账户后重新提交。",
    )


def _accept(slot: str, value: object) -> object:
    """把可信值写入控制槽：经 `contracts.taint.Slot` 构造做**类型层**把关。"""
    Slot(kind=SlotKind.CONTROL, value=Value(value, Trust.TRUSTED))
    return value


def _resolve_slot(
    slot: str, inputs: SlotInputs, *, bare_amount: bool = False
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    notes: list[str] = []
    # 页面把未选的下拉框以 `null` 提交：显式 null 与「不传」同口径，落到解析器 / 追问，
    # 不能因为多了一个空键就改口径（见 2026-10-27 验收记录 §5 缺陷 8）。
    if inputs.selected.get(slot) is not None:
        raw = inputs.selected[slot]
        if _is_trusted(raw):
            notes.append(f"selected:{slot}")
            value = _normalize_selected(
                slot,
                raw.raw if isinstance(raw, Value) else raw,
                inputs.today,
                bare_amount=bare_amount,
            )
            if value is not None:
                return value, None, tuple(notes)
            notes.append(f"invalid selected:{slot}")
            return None, SlotQuestion(slot, _PROMPTS[slot]), tuple(notes)
        notes.append(f"rejected unverified:{slot}")
    if slot in {"payee", "payer"}:
        return _resolve_payee(inputs, notes)
    if slot in {"amount", "amount_cents"}:
        return _resolve_amount(inputs, notes, slot, bare_amount=bare_amount)
    if slot == "daily_limit":
        return _resolve_daily_limit(inputs, notes)
    if slot == "when":
        return _resolve_when(inputs, notes)
    if slot == "period":
        periods = {
            canonical
            for alias, canonical in _PERIOD_ALIASES.items()
            if re.search(rf"\b{alias}\b" if alias.isascii() else alias, inputs.text)
        }
        if len(periods) == 1:
            return periods.pop(), None, tuple(notes)
    if slot == "merchant":
        # 只取明确的「给某商户开通」原文，商户缺失或修改指令仍需澄清。
        match = re.search(r"给(?P<merchant>[^，,。；;]{1,30}?)开通", inputs.text)
        if match is not None:
            return match["merchant"].strip(), None, tuple(notes)
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


def _normalize_selected(
    slot: str, raw: object, today: date, *, bare_amount: bool = False
) -> object | None:
    """把澄清表单的可信字符串转回工具要求的金额 / 周期 / 日期类型。"""
    if slot in {"amount", "amount_cents", "daily_limit"} and isinstance(raw, str):
        text = raw.strip()
        if slot == "amount_cents" and text.isdigit():
            return int(text)
        # 澄清表单里答一个「200」就是 200 元（与 `_resolve_amount` 同口径）；额度槽仍要求带单位。
        parsed_amount = parse_amount(text, bare_amounts=bare_amount and slot == "amount")
        return parsed_amount.value.cents if parsed_amount.value is not None else None
    if slot == "period" and isinstance(raw, str):
        return _PERIOD_ALIASES.get(raw.strip().lower())
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


def _resolve_amount(
    inputs: SlotInputs, notes: list[str], slot: str, *, bare_amount: bool = False
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    """金额槽：转账族允许**无歧义的裸数字**（「转给李娜 500」）；尾号 / 日期 / 序数仍不当金额。"""
    parsed = parse_amount(
        inputs.text,
        bare_amounts=bare_amount,
        reserved_digits=_reserved_digits(inputs.payees),
    )
    if not parsed.matched or parsed.value is None:
        notes.append(f"missing {slot}")
        return None, SlotQuestion(slot, _PROMPTS[slot]), tuple(notes)
    notes.append(f"resolved {slot}:{parsed.value.cents}")
    return parsed.value.cents, None, tuple(notes)


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
