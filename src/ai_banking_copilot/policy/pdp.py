"""三色策略引擎（PDP）：官方三色主干 + 单向升档增强层。

官方口径（H10 硬约束，不得偏离）：
- 绿色 = 自动执行，**仅纯查询**；任何资金 / 状态变更不得落入绿色。
- 黄色 = 用户确认；`日累计 ≤ 1000 元` ⇒ 黄。
- 红色 = 强验证；`日累计 > 1000 元` ⇒ 红。
- **客户级日限额**（银行侧下发）为**只加严**维度：有效阈值 = `min(官方 1000 元, 客户限额)`，
  官方口径**不可放宽**（客户数据只能更严，不能更松）。
- 增强层（红线之上的兜底）：`MANUAL` 人工接管 · `DENY` 拒绝并解释。

合成规则（fail-safe）：
- `base = 决策表(动作风险, 日累计档)`；`modifier` **只做单向升档**；
  `L_final = max(base, 各 modifier)`。
- 规则冲突取**更严**级别（`contracts.authorization.strictest`，并集原因，保证可解释）。
- 对象维度**只升档、不降档**；`blocked` 为**吸收态** ⇒ `DENY`（唯一可压制金额维度者）。
- 多候选收款人**不产出级别**（本层拒绝定级，强制澄清）。
- **金额下限保护**：**当日流出累计** > 1000 ⇒ L_final ≥ 红（仅 `amount_applies=True` 的请求），
  与对象维度无关。

本模块**纯函数、零 I/O**：不读库、不调模型、不发网络。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
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
"""官方日累计阈值（元）：`≤ 该值 ⇒ 黄`、`> 该值 ⇒ 红`（PRD §5.2）。

它同时是**官方上限**：客户级日限额只能把它收紧，不能放宽。"""

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

OFFICIAL_RED_ACTIONS: frozenset[str] = frozenset(
    {
        "wealth.subscribe",
        "wealth.redeem",
        "card.unlock",
        "card.set_limit",
        "card.replace",
        "card.adjust_credit_limit",
        "subscription.sign",
        "subscription.cancel",
        "transfer.request_collection",
        "password.change",
    }
)
"""官方明文点名红色、但按金额档 / 可逆性推不出红色的动作（官方口径不得偏离）。

- `wealth.subscribe`：官方红色示例「理财产品申购」——不因金额小而降为黄；
- `wealth.redeem`：赎回是持仓扣减与资金回流，演示环境按红色处理（只加严）；
  赎回确认面与申购共用服务端一次性验证码；
- `card.unlock`：官方红色「挂失 / 解挂」——解挂是可逆状态变更且无金额，按金额档会落到黄；
  解除保护比施加保护更危险，故强制 ≥ 红（`card.lock` 施加保护仍为黄）；
- `card.set_limit`：调高卡片限额是**解除既有风控上限**，可把卡片变成无额度约束的敞口，
  且无金额参数（按金额档只会落黄）——与「解挂」同属「解除保护」，强制 ≥ 红；
- `card.replace`：补卡换新卡号并邮寄到地址，属卡片生命周期变更，按红色要求强验证；
- `card.adjust_credit_limit`：提高信用额度是解除既有授信上限，固定额度还需审批，
  与 `card.set_limit` 同属「解除保护」，强制 ≥ 红；
- `subscription.sign`：代扣签约把**未来无限次扣款**授权给商户（单笔小但敞口长期、易被诈骗利用），
  按金额档次次落黄不足以覆盖风险，强制 ≥ 红；
- `subscription.cancel`：关闭的是银行卡侧扣款授权（解除既有支付授权），
  与「解挂 / 调高限额」同属解除保护；单笔金额小不代表敞口小，强制 ≥ 红；
- `transfer.request_collection`：发起收款即向他人**索款**，是电信诈骗的典型施压手段，
  金额小也不降级，强制 ≥ 红；
- `password.change`：官方红色示例「密码修改」——改密是账户被接管的最后一步，无金额参数，
  按金额档只会落绿，强制 ≥ 红。

依据：`docs/2026-10-26/三色基准表-2026-10-26.md` 第三节（2026-10-26 裁决）。
"""

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


class ObjectState(StrEnum):
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
    daily_limit: Money | None = None
    """**客户级日限额**（银行侧个性化；`None` = 用官方默认阈值）。

    官方口径**不可放宽**：有效阈值恒取 `min(官方默认, 客户限额)`，只能更严。
    """
    context_flags: frozenset[str] = field(default_factory=frozenset)
    amount_applies: bool = True
    """金额档是否适用：`False ⇒ 金额档不适用，仅用于非流出型动作`。

    仅当动作产生**资金流出**时为 `True`；非流出型动作（锁定卡、改限额、挂失等）
    即便 `daily_accumulated` 很高，也不得被金额档抬到红色。
    """

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
        _official_red_modifier(request.action),
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


def _effective_limit_cents(customer_limit: Money | None) -> int:
    """有效日限额 = `min(官方默认, 客户限额)`：官方口径不可放宽，客户数据只能更严。"""
    official = DAILY_LIMIT_YUAN * _CENT_PER_YUAN
    if customer_limit is None:
        return official
    return min(official, customer_limit.cents)


def _base_decision(request: PolicyRequest) -> Decision:
    if request.is_pure_query:
        return green_or_raise(is_pure_query=True)
    if not request.amount_applies:
        return Decision(level=Level.YELLOW, reasons=(ReasonCode.STATE_CHANGE_SMALL,))
    return _amount_band_decision(
        request.daily_accumulated, _effective_limit_cents(request.daily_limit)
    )


def _amount_band_decision(daily_accumulated: Money, limit_cents: int) -> Decision:
    if daily_accumulated.cents <= limit_cents:
        return Decision(level=Level.YELLOW, reasons=(ReasonCode.STATE_CHANGE_SMALL,))
    return Decision(level=Level.RED, reasons=(ReasonCode.AMOUNT_OVER_DAILY_LIMIT,))


def _official_red_modifier(action: str) -> Decision | None:
    """官方点名红色动作：无条件 ≥ 红（只升档，不降档）。"""
    if action in OFFICIAL_RED_ACTIONS:
        return Decision(level=Level.RED, reasons=(ReasonCode.OFFICIAL_RED,))
    return None


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
