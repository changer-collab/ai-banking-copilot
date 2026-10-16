"""订阅场景：取消 = **负未来流出**；竞态走 saga 补偿并**如实告知**（不宣称"已取消"）。

口径（PRD §4.1 场景⑤ / §5.3 / §5.5）：
- 取消订阅的 cf 语义 = `CANCEL_OUTFLOW`（未来流出记负）；
- 已进入结算的竞态**不得谎称已取消**，必须走补偿事务并如实告知"本期已扣、已发起退回"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    validate_plan,
)
from ai_banking_copilot.gateway.core_bank import WriteReceipt
from ai_banking_copilot.gateway.errors import GatewayNotFoundError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money, Subscription, Transaction
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.pep import (
    QueryContext,
    QueryHandler,
    QueryResult,
    WriteContext,
    WriteHandler,
    money_cents,
    require_date,
    require_str,
)
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


def upcoming_charges(gateway: FactGateway, user_id: str) -> tuple[Subscription, ...]:
    """只读到期提醒事实：按 `next_charge_on` **升序**返回用户全部订阅。"""
    records = gateway.subscriptions_of(user_id)
    return tuple(
        record.payload
        for record in sorted(records, key=lambda record: record.payload.next_charge_on)
    )


def describe_upcoming_charges(subscriptions: tuple[Subscription, ...]) -> str:
    """只陈述事实：订阅数 + 最近到期日 + 商户/金额（无收益/推荐措辞，守 H6/H3）。"""
    if not subscriptions:
        return "暂无订阅"
    nearest = subscriptions[0]
    return (
        f"共 {len(subscriptions)} 项，最近到期 {nearest.next_charge_on.isoformat()}"
        f"（{nearest.merchant} {nearest.amount.yuan} 元）"
    )


@dataclass(frozen=True)
class DetectedCharge:
    """从流水**归并识别**出的订阅扣费（同一商户的流出记录聚合）。"""

    merchant: str
    subscription_id: str | None
    txn_count: int
    total: Money
    last_booked_on: date


def detected_charges(gateway: FactGateway, user_id: str) -> tuple[DetectedCharge, ...]:
    """自动识别订阅扣费：把**流出流水**按订阅商户名归并（官方场景⑤「自动识别订阅扣费」）。

    判据是**商户名精确匹配**已知订阅——不是模型推断，故可复现、可解释；识别不到的订阅
    一律不返回（不编造扣费），非订阅商户也不返回。
    """
    by_merchant = {
        record.payload.merchant: record.payload for record in gateway.subscriptions_of(user_id)
    }
    if not by_merchant:
        return ()
    grouped: dict[str, list[Transaction]] = {}
    for account in gateway.accounts_of(user_id):
        for record in gateway.transactions_between(
            account.payload.account_id, date.min, date.max
        ):
            txn = record.payload
            if txn.direction == "out" and txn.merchant in by_merchant:
                grouped.setdefault(txn.merchant, []).append(txn)
    detected = (
        DetectedCharge(
            merchant=merchant,
            subscription_id=by_merchant[merchant].subscription_id,
            txn_count=len(items),
            total=Money(sum(abs(item.amount.cents) for item in items)),
            last_booked_on=max(item.booked_on for item in items),
        )
        for merchant, items in grouped.items()
    )
    return tuple(sorted(detected, key=lambda charge: (-charge.total.cents, charge.merchant)))


def describe_detected_charges(charges: tuple[DetectedCharge, ...]) -> str:
    """只陈述事实：识别到的项数、笔数与合计金额（无推荐/收益措辞，守 H6）。"""
    if not charges:
        return "未从流水中识别到订阅扣费"
    grand_total = Money(sum(charge.total.cents for charge in charges))
    detail = "；".join(
        f"{charge.merchant} {charge.txn_count} 笔合计 {charge.total.yuan} 元"
        f"（最近 {charge.last_booked_on.isoformat()}）"
        for charge in charges
    )
    return f"识别到 {len(charges)} 项订阅扣费，合计 {grand_total.yuan} 元：{detail}"


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


def _cancel_handler(context: WriteContext) -> WriteReceipt:
    """把 `subscription.cancel` 映射到 `CoreBank.cancel_subscription`。"""
    return context.core_bank.cancel_subscription(
        subscription_id=require_str(context.params, "subscription_id"),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


def _sign_handler(context: WriteContext) -> WriteReceipt:
    """把 `subscription.sign` 映射到 `CoreBank.sign_subscription`。"""
    return context.core_bank.sign_subscription(
        user_id=require_str(context.params, "user_id"),
        merchant=require_str(context.params, "merchant"),
        amount=Money(money_cents(context.params, context.spec)),
        period=require_str(context.params, "period"),
        next_charge_on=require_date(context.params, "next_charge_on"),
        idempotency_key=context.idempotency_key,
        before_write=context.before_write,
    )


WRITE_HANDLERS: Mapping[str, WriteHandler] = {
    "subscription.cancel": _cancel_handler,
    "subscription.sign": _sign_handler,
}
"""订阅域写处理器（组合根汇入 PEP；`docs/已修复清单-2026-10-08.md` 步骤 1.5）。"""


def _subscription_list(context: QueryContext) -> QueryResult:
    subscriptions = context.gateway.subscriptions_of(require_str(context.params, "user_id"))
    return QueryResult(
        record_ids=tuple(item.record_id for item in subscriptions),
        provenance=tuple((item.record_id, item.content_hash) for item in subscriptions),
        detail=f"共 {len(subscriptions)} 项订阅",
    )


QUERY_HANDLERS: Mapping[str, QueryHandler] = {"subscription.list": _subscription_list}
"""订阅域**读**处理器（组合根汇入 PEP；`docs/已修复清单-2026-10-08.md` 步骤 4「残留」补做）。"""
