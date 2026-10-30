"""计划级净效应（NE）：单步合法 ≠ 整体正确（PRD §5.3 第 1 条）。

`cf` 口径（**带符号资金流，以"流出为正"**）：
- 转账 / 定时转账 / 理财申购 / 代扣签约 / 自动续费 = `+流出`；
- 理财赎回 = `−流出`；
- **冻结 / 解冻 = `0`（净效应中性）**：二者只是把同一笔钱在「可用余额 ↔ 冻结额」之间搬，
  真正的流出发生在消费节点（如 `merchant.order`）。若把冻结记正、解冻记负，
  「冻结 1000 → 下单 299 → 解冻 701」会被算成 1000 + 299 − 701 = 598，凭空多出 299；
  中性化后净流出恰为 299，与计划卡「净流出 299 元」一致。
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


NEUTRAL_NET_ACTIONS = frozenset({"funds.freeze", "funds.release"})
"""净效应中性动作：只在「可用余额 ↔ 冻结额」之间搬钱，不计入计划级净流出。

三处净效应实现（此处 / `scenarios.base.plan_net_effect` / `policy.execution.assess_plan`）
必须同向跳过，否则计划卡与执行期判档会打架。
"""


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
        if node.action in NEUTRAL_NET_ACTIONS:
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
