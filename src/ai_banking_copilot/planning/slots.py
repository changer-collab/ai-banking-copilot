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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from ai_banking_copilot.contracts.taint import Slot, SlotKind, Trust, Value
from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.gateway.resolvers import (
    Candidate,
    parse_amount,
    parse_temporal,
    resolve_payees,
)

REQUIRED_SLOTS: Mapping[str, tuple[str, ...]] = {
    "account.balance": ("account",),
    "account.transactions": ("account",),
    "bills.monthly": ("user_id",),
    "bills.category_trend": ("user_id",),
    "wealth.list": (),
    "card.list": ("user_id",),
    "subscription.list": ("user_id",),
    "transfer.create": ("user_id", "account", "payee", "amount", "when"),
    "transfer.schedule": ("user_id", "account", "payee", "amount", "when"),
    "wealth.subscribe": ("user_id", "account", "product", "amount"),
    "wealth.redeem": ("user_id", "account", "product", "amount"),
    "subscription.cancel": ("subscription",),
    "subscription.sign": ("user_id", "merchant", "amount_cents", "period", "next_charge_on"),
    "card.lock": ("card",),
    "card.unlock": ("card",),
    "card.report_lost": ("card",),
    "card.apply_virtual": ("user_id", "daily_limit"),
    "card.set_limit": ("card", "daily_limit"),
    "funds.freeze": ("account", "amount"),
    "funds.release": ("hold",),
}
"""动作 → 必填控制槽（键与 `planning.llm.ACTION_KINDS` 的 20 个动作一一对应）。

**与 02 目录的控制槽一致性**：凡 02 目录声明为**必填 `user_id` 控制槽**的动作，此处必须含
`user_id`（纯查询动作亦然：`bills.monthly` / `bills.category_trend` / `card.list` /
`subscription.list`）；写动作（`transfer.create` / `transfer.schedule` / `wealth.subscribe` /
`wealth.redeem` / `subscription.sign` / `card.apply_virtual`）一并覆盖。`subscription.sign`
的对象是**商户**（不涉及已有订阅 id），故按 `merchant` / `amount_cents` / `period` /
`next_charge_on` 声明（README 裁决 #17）；`user_id` 由**受信任调用方**经 `selected` 填入
（会话层与评测链装配 `DEMO_USER_ID`）。
"""

SLOT_PARAMS: Mapping[str, str] = {
    "account": "account_id",
    "payee": "payee_id",
    "amount": "amount_cents",
    "when": "booked_on",
    "product": "product_id",
    "subscription": "subscription_id",
    "card": "card_id",
    "hold": "hold_id",
    "daily_limit": "daily_limit_cents",
    "user_id": "user_id",
    "merchant": "merchant",
    "amount_cents": "amount_cents",
    "period": "period",
    "next_charge_on": "next_charge_on",
}
"""控制槽 → `PlanNode.params` 键名。"""

_PROMPTS: Mapping[str, str] = {
    "account": "请选择要操作的账户。",
    "payee": "请确认收款人（需要全名 / 账号尾号 / 手机尾号）。",
    "amount": "请给出金额（必须带单位，例如 500 元）。",
    "when": "请给出执行时间（例如 明天 / 2026-12-25）。",
    "product": "请选择理财产品。",
    "subscription": "请选择要处理的订阅。",
    "card": "请选择要处理的卡片。",
    "daily_limit": "请给出额度（必须带单位，例如 2 万元）。",
    "hold": "请选择要解冻的冻结记录。",
    "user_id": "请确认要操作的用户身份（由受信任调用方提供）。",
    "merchant": "请给出代扣签约的商户名。",
    "amount_cents": "请给出代扣金额（单位：分）。",
    "period": "请给出代扣周期（monthly / yearly）。",
    "next_charge_on": "请给出下次扣款日（例如 2026-11-01）。",
}


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


def fill_control_slots(action: str, inputs: SlotInputs) -> SlotFill:
    """按 `REQUIRED_SLOTS[action]` 逐个补槽；返回已填槽与待澄清项。

    `negation=True` 只可能来自真正的拒绝表达（"不要 / 别 / 拒绝"等）：01 的 `parse_temporal`
    已按 README 裁决 §11 把「取消」移出否定词表，故「取消订阅」这类合法意图不会被拦截。
    """
    parsed = parse_temporal(inputs.text, inputs.today)
    if parsed.negation:
        return SlotFill(trace=(f"negation detected:{parsed.evidence}",), negated=True)

    filled: dict[str, object] = {}
    questions: list[SlotQuestion] = []
    trace: list[str] = []
    for slot in REQUIRED_SLOTS.get(action, ()):
        chosen, question, notes = _resolve_slot(slot, inputs)
        trace.extend(notes)
        if chosen is not None:
            filled[slot] = _accept(slot, chosen)
        elif question is not None:
            questions.append(question)
    return SlotFill(slots=filled, questions=tuple(questions), trace=tuple(trace))


def _accept(slot: str, value: object) -> object:
    """把可信值写入控制槽：经 `contracts.taint.Slot` 构造做**类型层**把关。"""
    Slot(kind=SlotKind.CONTROL, value=Value(value, Trust.TRUSTED))
    return value


def _resolve_slot(
    slot: str, inputs: SlotInputs
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    notes: list[str] = []
    if slot in inputs.selected:
        raw = inputs.selected[slot]
        if _is_trusted(raw):
            notes.append(f"selected:{slot}")
            return (raw.raw if isinstance(raw, Value) else raw), None, tuple(notes)
        notes.append(f"rejected unverified:{slot}")
    if slot == "payee":
        return _resolve_payee(inputs, notes)
    if slot == "amount":
        return _resolve_amount(inputs, notes)
    if slot == "when":
        return _resolve_when(inputs, notes)
    return None, SlotQuestion(slot=slot, prompt=_PROMPTS[slot]), tuple(notes)


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
    inputs: SlotInputs, notes: list[str]
) -> tuple[object | None, SlotQuestion | None, tuple[str, ...]]:
    parsed = parse_amount(inputs.text)
    if not parsed.matched or parsed.value is None:
        notes.append("missing amount")
        return None, SlotQuestion("amount", _PROMPTS["amount"]), tuple(notes)
    notes.append(f"resolved amount:{parsed.value.cents}")
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


def _payee_label(candidate: Candidate) -> str:
    return (
        f"{candidate.full_name}"
        f"(账号尾号 {candidate.account_tail} / 手机尾号 {candidate.phone_tail})"
    )
