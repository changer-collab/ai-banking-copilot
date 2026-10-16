"""完整计划的执行时风险计算与已提交错误。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from ai_banking_copilot.contracts.authorization import Decision, Level, strictest
from ai_banking_copilot.contracts.plan import Plan, PlanNode, validate_plan
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE, WriteReceipt
from ai_banking_copilot.gateway.errors import GatewayError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.transaction_facts import TransactionFacts
from ai_banking_copilot.planning.llm import ACTION_KINDS, ActionKind
from ai_banking_copilot.planning.net_effect import NetEffect
from ai_banking_copilot.policy.pdp import DAILY_LIMIT_YUAN, evaluate, evaluate_plan
from ai_banking_copilot.scenarios.authorization import AuthorizationFacts, node_policy_request
from ai_banking_copilot.tools.registry import ToolRegistry


@dataclass(frozen=True)
class ExecutionContext:
    execution_id: str
    frozen_hash: str
    user_id: str
    today: date
    approved_level: Level
    strong_verified: bool = False
    completed_records: tuple[str, ...] = ()
    flags: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PlanRisk:
    decision: Decision
    node_decisions: tuple[Decision, ...]
    limit_cents: int
    accumulated_cents: int


class CommittedWriteError(GatewayError):
    def __init__(self, receipt: WriteReceipt, cause: BaseException) -> None:
        self.receipt = receipt
        super().__init__(f"业务已提交，后续核验或审计失败：{cause}")


def assess_plan(
    plan: Plan,
    *,
    registry: ToolRegistry,
    gateway: AuthorizationFacts,
    user_id: str,
    today: date,
    flags: frozenset[str] = frozenset(),
    completed_records: tuple[str, ...] = (),
    floor: Decision | None = None,
) -> PlanRisk:
    validate_plan(plan)
    outflow = 0
    net_cents = 0
    irreversibles: list[str] = []
    settlement_days: set[date] = set()
    for node in plan.nodes:
        kind = ACTION_KINDS.get(node.action)
        if kind is None:
            raise ValueError(f"未知计划动作：{node.action}")
        if kind is ActionKind.OUTFLOW:
            spec = registry.get(node.action)
            raw = node.params.get(spec.money_param or "amount_cents")
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ValueError(f"节点 {node.node_id} 缺少有效流出金额")
            outflow += raw
            net_cents += raw
            settlement_days.add(_settlement_day(node, today))
        elif kind is ActionKind.INFLOW:
            if node.action == "funds.release":
                hold_id = node.params.get("hold_id")
                if not isinstance(hold_id, str):
                    raise ValueError(f"节点 {node.node_id} 缺少冻结记录")
                net_cents -= gateway.hold(hold_id).payload.amount.cents
            else:
                raw = node.params.get("amount_cents")
                if isinstance(raw, bool) or not isinstance(raw, int):
                    raise ValueError(f"节点 {node.node_id} 缺少有效金额")
                net_cents -= raw
        elif kind is ActionKind.CANCEL_OUTFLOW:
            raw = node.params.get("subscription_id")
            if not isinstance(raw, str):
                raise ValueError(f"节点 {node.node_id} 缺少订阅标识")
            net_cents -= gateway.subscription(raw).payload.amount.cents
        if kind is ActionKind.IRREVERSIBLE:
            irreversibles.append(node.node_id)
    if not settlement_days:
        settlement_days.add(today)
    accumulated_by_day = {
        day: (
            gateway.daily_transfer_total(user_id, day).cents
            - _completed_transfer_cents(gateway, user_id, day, completed_records)
            + outflow
        )
        for day in settlement_days
    }
    accumulated = max(accumulated_by_day.values())
    limit = min(DAILY_LIMIT_YUAN * 100, gateway.daily_transfer_limit(user_id).cents)
    decisions: list[Decision] = []
    for node in plan.nodes:
        spec = registry.get(node.action)
        day_decisions: list[Decision] = []
        for day, daily_accumulated in accumulated_by_day.items():
            request = node_policy_request(
                node, spec=spec, gateway=gateway, user_id=user_id, today=day, flags=flags
            )
            if request.amount_applies:
                request = replace(request, daily_accumulated=Money(daily_accumulated))
            day_decisions.append(evaluate(request))
        decisions.append(strictest(*day_decisions))
    if not decisions:
        raise ValueError("空计划不可评估")
    if any(not registry.get(node.action).is_pure_query for node in plan.nodes):
        decisions.append(evaluate_plan(
            plan, NetEffect(Money(net_cents), tuple(irreversibles),
                            tuple(node.node_id for node in plan.nodes))
        ))
    if floor is not None:
        decisions.append(floor)
    return PlanRisk(
        decision=strictest(*decisions),
        node_decisions=tuple(decisions[:len(plan.nodes)]),
        limit_cents=limit,
        accumulated_cents=accumulated,
    )


def _settlement_day(node: PlanNode, today: date) -> date:
    if node.action not in ("transfer.create", "transfer.schedule"):
        return today
    if "schedule_id" in node.params:
        raw = node.params.get("executed_on", DEFAULT_BOOKING_DATE)
    else:
        raw = node.params.get("booked_on")
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        return date.fromisoformat(raw)
    raise ValueError(f"节点 {node.node_id} 缺少有效结算日")


def _completed_transfer_cents(
    gateway: AuthorizationFacts, user_id: str, today: date, record_ids: tuple[str, ...]
) -> int:
    if isinstance(gateway, TransactionFacts):
        return gateway.completed_transfer_total(user_id, today, record_ids).cents
    if not isinstance(gateway, FactGateway):
        raise TypeError("不支持的事实读取器")
    total = 0
    for record_id in set(record_ids):
        if not record_id.startswith("transactions:"):
            continue
        txn = gateway.transaction(record_id.removeprefix("transactions:")).payload
        if (txn.user_id == user_id and txn.booked_on == today
                and txn.category == "转账" and txn.direction == "out"):
            total -= txn.amount.cents
    return total
