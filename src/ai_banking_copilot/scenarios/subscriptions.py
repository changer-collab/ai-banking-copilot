"""订阅场景：取消 = **负未来流出**；竞态走 saga 补偿并**如实告知**（不宣称"已取消"）。

口径（PRD §4.1 场景⑤ / §5.3 / §5.5）：
- 取消订阅的 cf 语义 = `CANCEL_OUTFLOW`（未来流出记负）；
- 已进入结算的竞态**不得谎称已取消**，必须走补偿事务并如实告知"本期已扣、已发起退回"。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    validate_plan,
)
from ai_banking_copilot.gateway.errors import GatewayNotFoundError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money, Subscription
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.saga import ReversalOutcome, ReversalWindow, Saga
from ai_banking_copilot.scenarios.base import (
    ScenarioHandler,
    ScenarioRefused,
    as_date,
    as_money,
    as_str,
    grant_anchor,
    trusted_slot,
)

SUBSCRIPTION_ACTIONS = frozenset({"subscription.list", "subscription.cancel", "subscription.sign"})
"""订阅域动作（README 规范动作表 #7 / #12 / #13）。"""

_SIGN_PERIODS = frozenset({"monthly", "yearly"})
"""代扣周期白名单（与 02 目录 `subscription.sign` 的 `period` 约定一致）。"""


def future_outflow_effect(subscription: Subscription) -> Money:
    """取消订阅的 cf 口径：**负未来流出**（−本期名义额）。"""
    return Money(-subscription.amount.cents)


def resolve_cancel_race(
    window: ReversalWindow, saga: Saga, *, now: datetime, settled: bool
) -> ReversalOutcome:
    """取消竞态：已结算 → 走 saga 补偿并**如实告知**（不宣称"已取消"）。"""
    return window.reverse(now=now, settled=settled, compensate=saga.compensate)


class SubscriptionsHandler(ScenarioHandler):
    """订阅场景处理器。"""

    @property
    def name(self) -> str:
        return "subscriptions"

    def matches(self, intents: Sequence[Intent]) -> bool:
        return any(intent.action in SUBSCRIPTION_ACTIONS for intent in intents)

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        subscription_intents = [item for item in intents if item.action in SUBSCRIPTION_ACTIONS]
        if not subscription_intents:
            raise ScenarioRefused("未识别到订阅意图")
        ledger = AnchorLedger()
        nodes = tuple(
            _build_subscription_node(f"n{index}", intent, gateway, ledger)
            for index, intent in enumerate(subscription_intents, start=1)
        )
        plan = Plan(plan_id=f"subscriptions_{len(nodes)}", nodes=nodes)
        validate_plan(plan)
        return plan


def _build_subscription_node(
    node_id: str, intent: Intent, gateway: FactGateway, ledger: AnchorLedger
) -> PlanNode:
    detail = f"订阅操作：{intent.evidence or intent.action}"
    anchor_id = grant_anchor(ledger, AnchorSource.EXPLICIT, detail)
    if intent.action == "subscription.list":
        params: dict[str, object] = {"user_id": DEMO_USER_ID}
    elif intent.action == "subscription.sign":
        params = _sign_params(intent)
    else:
        subscription_id = _subscription_id(intent)
        _require_subscription(gateway, subscription_id)
        params = {"subscription_id": subscription_id}
    return PlanNode(
        node_id=node_id,
        action=intent.action,
        params=params,
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id=anchor_id),
    )


def _sign_params(intent: Intent) -> dict[str, object]:
    """代扣签约装配（README 裁决 #17）：对象是**商户**，不涉及已有订阅 id。"""
    period = as_str(trusted_slot(intent, ("period",), "period"), "period")
    if period not in _SIGN_PERIODS:
        raise ScenarioRefused(f"代扣周期只支持 {sorted(_SIGN_PERIODS)}，收到 {period}")
    return {
        "user_id": DEMO_USER_ID,
        "merchant": as_str(trusted_slot(intent, ("merchant",), "merchant"), "merchant"),
        "amount_cents": as_money(
            trusted_slot(intent, ("amount", "amount_cents"), "amount"), "amount"
        ).cents,
        "period": period,
        "next_charge_on": as_date(
            trusted_slot(intent, ("next_charge_on", "when"), "next_charge_on"),
            "next_charge_on",
        ).isoformat(),
    }


def _subscription_id(intent: Intent) -> str:
    return as_str(
        trusted_slot(intent, ("subscription", "subscription_id"), "subscription"),
        "subscription",
    )


def _require_subscription(gateway: FactGateway, subscription_id: str) -> None:
    try:
        gateway.subscription(subscription_id)
    except GatewayNotFoundError as error:
        raise ScenarioRefused(f"订阅不存在：{subscription_id}") from error
