"""计划级净效应（NE）：单步合法 ≠ 整体正确（PRD §5.3 第 1 条）。

`cf` 口径（**带符号资金流，以"流出为正"**）：
- 转账 / 定时转账 / 理财申购 / 代扣签约 / 自动续费 / 冻结资金 = `+流出`；
- 理财赎回 / 解冻 = `−流出`；
- 订阅取消 / 解约 = `−未来流出`（未来流出记负）；
- 挂失 = **不可逆**，**不入 cf**，单列进 `irreversibles`（触发 ≥ 红色 + L3）；
- 纯查询 / 非不可逆状态变更 = `0`。

计划级阈值（`NE ≥ T_net ⇒ 计划级 ≥ L2`）由 `policy.pdp::evaluate_plan` 使用，本模块只负责算数值。

**fail-safe 取数**：未知动作、缺 `amount_cents`、取消订阅缺 `subscription_id` 一律**拒绝计算**
（宁可报错，也不把"算不出的流出"当成 0）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_banking_copilot.contracts.plan import Plan, PlanNode
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.planning.llm import ACTION_KINDS, ActionKind


class NetEffectError(ValueError):
    """节点缺少可计算的资金流参数，或动作不在规范词表内。"""


@dataclass(frozen=True)
class NetEffect:
    """计划级净效应。`amount` 为**净流出**（正 = 净流出）。"""

    amount: Money
    irreversibles: tuple[str, ...]
    node_ids: tuple[str, ...]


def net_effect(plan: Plan, gateway: FactGateway) -> NetEffect:
    """按 `ACTION_KINDS` 汇总全计划带符号资金流。"""
    total = 0
    irreversibles: list[str] = []
    node_ids: list[str] = []
    for node in plan.nodes:
        node_ids.append(node.node_id)
        kind = ACTION_KINDS.get(node.action)
        if kind is None:
            raise NetEffectError(f"未知动作 {node.action}，拒绝计算净效应")
        if kind is ActionKind.IRREVERSIBLE:
            irreversibles.append(node.node_id)
            continue
        if kind in (ActionKind.QUERY, ActionKind.STATE):
            continue
        if kind is ActionKind.CANCEL_OUTFLOW:
            total -= _subscription_cents(node, gateway)
            continue
        cents = _amount_cents(node)
        total += cents if kind is ActionKind.OUTFLOW else -cents
    return NetEffect(
        amount=Money(total),
        irreversibles=tuple(irreversibles),
        node_ids=tuple(node_ids),
    )


def _amount_cents(node: PlanNode) -> int:
    raw = node.params.get("amount_cents")
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise NetEffectError(f"节点 {node.node_id} 缺少整数 amount_cents")
    return raw


def _subscription_cents(node: PlanNode, gateway: FactGateway) -> int:
    raw = node.params.get("subscription_id")
    if not isinstance(raw, str) or not raw:
        raise NetEffectError(f"节点 {node.node_id}（订阅取消）缺少 subscription_id")
    return gateway.subscription(raw).payload.amount.cents
