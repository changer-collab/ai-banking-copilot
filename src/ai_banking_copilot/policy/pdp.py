"""三色策略引擎（PDP）：官方三色主干 + 单向升档增强层。

官方口径（H10 硬约束，不得偏离）：
- 绿色 = 自动执行，**仅纯查询**；任何资金 / 状态变更不得落入绿色。
- 黄色 = 用户确认；`日累计 ≤ 1000 元` ⇒ 黄。
- 红色 = 强验证；`日累计 > 1000 元` ⇒ 红。
- 增强层（红线之上的兜底）：`MANUAL` 人工接管 · `DENY` 拒绝并解释。

合成规则（fail-safe）：
- `base = 决策表(动作风险, 日累计档)`；`modifier` **只做单向升档**；
  `L_final = max(base, 各 modifier)`。
- 规则冲突取**更严**级别（`contracts.authorization.strictest`，并集原因，保证可解释）。
- 对象维度**只升档、不降档**；`blocked` 为**吸收态** ⇒ `DENY`（唯一可压制金额维度者）。
- 多候选收款人**不产出级别**（本层拒绝定级，强制澄清）。
- **金额下限保护**：`日累计 > 1000 ⇒ L_final ≥ 红`，与对象维度无关。

本模块**纯函数、零 I/O**：不读库、不调模型、不发网络。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from ai_banking_copilot.contracts.authorization import (
    Decision,
    Level,
    ReasonCode,
    green_or_raise,
    strictest,
)
from ai_banking_copilot.contracts.plan import Plan, validate_plan
from ai_banking_copilot.gateway.models import Money

DAILY_LIMIT_YUAN: int = 1000
"""官方日累计阈值（元）：`≤ 该值 ⇒ 黄`、`> 该值 ⇒ 红`（PRD §5.2）。"""

NET_EFFECT_THRESHOLD_YUAN: int = 1000
"""计划级净效应阈值 `T_net`（元）：`NE ≥ T_net ⇒ 计划级 ≥ 红`（PRD §5.3）。"""

FLAG_OFF_HOURS = "off_hours"
"""非惯常时段。"""

FLAG_DEVICE_CHANGE = "device_change"
"""会话设备 / 地点跳变。"""

FLAG_HIGH_FREQUENCY = "high_frequency"
"""短时高频高风险操作。"""

FLAG_INJECTION_DETECTED = "injection_detected"
"""注入检测命中（H3 / 官方"注入防御"）。"""

FLAG_FIRST_LARGE_TO_OBJECT = "first_large_to_object"
"""首次向某对象大额（涵盖熟人社工 / 盗号）。"""

KNOWN_CONTEXT_FLAGS: frozenset[str] = frozenset(
    {
        FLAG_OFF_HOURS,
        FLAG_DEVICE_CHANGE,
        FLAG_HIGH_FREQUENCY,
        FLAG_INJECTION_DETECTED,
        FLAG_FIRST_LARGE_TO_OBJECT,
    }
)
"""上下文异常标记白名单：未登记标记一律拒绝，防"凭空加载荷"。"""

_CENT_PER_YUAN = 100
_YELLOW_CONTEXT_FLAGS = frozenset({FLAG_OFF_HOURS, FLAG_DEVICE_CHANGE, FLAG_HIGH_FREQUENCY})


class ObjectState(str, Enum):
    """对象态机（PRD §5.2，唯一谓词求值结果）。`BLOCKED` 为吸收态。"""

    SELF = "self"
    KNOWN_UNVERIFIED = "known_unverified"
    VERIFIED_SAVED = "verified_saved"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"


class AmbiguousObjectError(ValueError):
    """收款对象候选 ≥ 2：**不产出级别**，强制澄清。"""


@dataclass(frozen=True)
class PolicyRequest:
    """一次单对象裁决请求（纯数据）。

    `daily_accumulated` = **含本笔**的当日累计流出额（与 `daily_transfer_total` 同口径）。
    """

    user_id: str
    action: str
    amount: Money
    daily_accumulated: Money
    object_state: ObjectState
    is_pure_query: bool
    irreversible: bool
    context_flags: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.object_state, ObjectState):
            raise TypeError(
                f"object_state 必须是 ObjectState 成员，收到 {type(self.object_state).__name__}"
            )
        if not self.user_id or not self.action:
            raise ValueError("user_id 与 action 不得为空")
        if self.amount.cents < 0:
            raise ValueError("amount 不得为负")
        if self.daily_accumulated.cents < self.amount.cents:
            raise ValueError("daily_accumulated 必须包含本笔金额")
        unknown = self.context_flags - KNOWN_CONTEXT_FLAGS
        if unknown:
            raise ValueError(f"未知上下文标记：{sorted(unknown)}")


def evaluate(request: PolicyRequest) -> Decision:
    """单对象裁决：官方三色主干 + 单向升档，冲突取更严（fail-safe）。"""
    if request.object_state is ObjectState.BLOCKED:
        return Decision(level=Level.DENY, reasons=(ReasonCode.OBJECT_BLOCKED,))
    modifiers: list[Decision | None] = [
        _object_modifier(request.object_state),
        _irreversible_modifier(request.irreversible),
        *_context_modifiers(request.context_flags),
    ]
    effective = tuple(item for item in modifiers if item is not None)
    return strictest(_base_decision(request), *effective)


def reject_ambiguous(candidate_count: int) -> None:
    """多候选收款人**不产出级别**：候选 ≥ 2 时抛错，强制澄清（PRD §5.1）。"""
    if candidate_count >= 2:
        raise AmbiguousObjectError(f"收款对象候选 {candidate_count} 个：不产出级别，强制澄清")


class NetEffectView(Protocol):
    """`planning.NetEffect` 的结构化视图（保持 03 不依赖 04）。

    口径（PRD §5.3）：`amount` 为**带符号资金流**的净效应 `NE`（流出为正、取消为负）；
    `irreversibles` 为不可逆节点 id（挂失等，单列触发 ≥ 红）。
    """

    @property
    def amount(self) -> Money: ...

    @property
    def irreversibles(self) -> tuple[str, ...]: ...


def evaluate_plan(plan: Plan, net: NetEffectView) -> Decision:
    """计划级裁决：净效应阈值 + 不可逆节点（防"多步组合越权"）。

    只产出计划级**下限**：纯查询计划的绿色判定由单节点 `evaluate`（`is_pure_query`）承担，
    计划级最终级别 = `strictest(各节点裁决, 本函数下限)`。
    """
    validate_plan(plan)
    decisions: list[Decision] = [_net_effect_decision(net.amount)]
    if net.irreversibles:
        decisions.append(Decision(level=Level.RED, reasons=(ReasonCode.IRREVERSIBLE,)))
    return strictest(*decisions)


def _net_effect_decision(net_amount: Money) -> Decision:
    if net_amount.cents >= NET_EFFECT_THRESHOLD_YUAN * _CENT_PER_YUAN:
        return Decision(level=Level.RED, reasons=(ReasonCode.AMOUNT_OVER_DAILY_LIMIT,))
    return Decision(level=Level.YELLOW, reasons=(ReasonCode.STATE_CHANGE_SMALL,))


def _base_decision(request: PolicyRequest) -> Decision:
    if request.is_pure_query:
        return green_or_raise(is_pure_query=True)
    return _amount_band_decision(request.daily_accumulated)


def _amount_band_decision(daily_accumulated: Money) -> Decision:
    if daily_accumulated.cents <= DAILY_LIMIT_YUAN * _CENT_PER_YUAN:
        return Decision(level=Level.YELLOW, reasons=(ReasonCode.STATE_CHANGE_SMALL,))
    return Decision(level=Level.RED, reasons=(ReasonCode.AMOUNT_OVER_DAILY_LIMIT,))


def _object_modifier(state: ObjectState) -> Decision | None:
    """陌生 / 未认人对象 ⇒ 追加一次认人确认（**只升档**，≥ 黄）。"""
    if state in (ObjectState.KNOWN_UNVERIFIED, ObjectState.UNKNOWN):
        return Decision(level=Level.YELLOW, reasons=(ReasonCode.MANUAL_REVIEW,))
    return None


def _irreversible_modifier(irreversible: bool) -> Decision | None:
    if irreversible:
        return Decision(level=Level.RED, reasons=(ReasonCode.IRREVERSIBLE,))
    return None


def _context_modifiers(flags: frozenset[str]) -> tuple[Decision, ...]:
    modifiers: list[Decision] = []
    if FLAG_INJECTION_DETECTED in flags:
        modifiers.append(Decision(level=Level.MANUAL, reasons=(ReasonCode.MANUAL_REVIEW,)))
    if flags & _YELLOW_CONTEXT_FLAGS:
        modifiers.append(Decision(level=Level.YELLOW, reasons=(ReasonCode.MANUAL_REVIEW,)))
    if FLAG_FIRST_LARGE_TO_OBJECT in flags:
        modifiers.append(Decision(level=Level.RED, reasons=(ReasonCode.AMOUNT_OVER_DAILY_LIMIT,)))
    return tuple(modifiers)
