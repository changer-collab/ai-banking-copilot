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
    if slot in {"payee", "payer"}:
        return _resolve_payee(inputs, notes)
    if slot == "amount":
        return _resolve_amount(inputs, notes)
    if slot == "daily_limit":
        return _resolve_daily_limit(inputs, notes)
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


def _payee_label(candidate: Candidate) -> str:
    return (
        f"{candidate.full_name}"
        f"(账号尾号 {candidate.account_tail} / 手机尾号 {candidate.phone_tail})"
    )
