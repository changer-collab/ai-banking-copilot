"""Reviewer 的最小网关出处摘要；回读采纳槽位和计划节点引用的对象。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from ai_banking_copilot.contracts.plan import Plan
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.planning.slots import SLOT_PARAMS
from ai_banking_copilot.scenarios.bills import (
    BILLS_ACTIONS,
    _chosen_category,
    _period,
    _previous_month,
)

_OBJECT_SLOTS = ("account", "payee", "payer", "card", "subscription", "product", "hold")
_OBJECT_TABLES = {
    "account": "accounts", "payee": "payees", "payer": "payees", "card": "cards",
    "subscription": "subscriptions", "product": "wealth_products", "hold": "holds",
}


def referenced_facts(
    gateway: FactGateway, user_id: str, intents: Sequence[Mapping[str, object]], plan: Plan,
) -> list[dict[str, object]]:
    references: dict[str, set[str]] = {}
    for intent in intents:
        slots = intent.get("slots")
        if not isinstance(slots, dict):
            continue
        for slot in _OBJECT_SLOTS:
            value = slots.get(slot)
            if isinstance(value, str):
                references.setdefault(slot, set()).add(value)
    # 计划参数只用于定位对象，记录内容必须独立回读；到期路径没有 intents。
    for node in plan.nodes:
        for slot in _OBJECT_SLOTS:
            value = node.params.get(SLOT_PARAMS[slot])
            if isinstance(value, str):
                references.setdefault(slot, set()).add(value)

    facts: list[dict[str, object]] = []
    for account_record in gateway.accounts_of(user_id):
        account = account_record.payload
        if account.account_id in references.get("account", set()):
            facts.append({"source": "gateway", "record_id": account_record.record_id,
                          "content_hash": account_record.content_hash,
                          "account_id": account.account_id,
                          "user_id": account.user_id, "kind": account.kind})
    for payee_record in gateway.payees_of(user_id):
        payee = payee_record.payload
        if payee.payee_id in references.get("payee", set()) | references.get("payer", set()):
            facts.append({"source": "gateway", "record_id": payee_record.record_id,
                          "content_hash": payee_record.content_hash, "payee_id": payee.payee_id,
                          "user_id": payee.user_id, "full_name": payee.full_name,
                          "identity_confirmed": payee.identity_confirmed, "blocked": payee.blocked})
    for card_record in gateway.cards_of(user_id):
        card = card_record.payload
        if card.card_id in references.get("card", set()):
            facts.append({"source": "gateway", "record_id": card_record.record_id,
                          "content_hash": card_record.content_hash, "card_id": card.card_id,
                          "user_id": card.user_id, "card_type": card.card_type,
                          "status": card.status})
    for subscription_record in gateway.subscriptions_of(user_id):
        subscription = subscription_record.payload
        if subscription.subscription_id in references.get("subscription", set()):
            facts.append({"source": "gateway", "record_id": subscription_record.record_id,
                          "content_hash": subscription_record.content_hash,
                          "subscription_id": subscription.subscription_id,
                          "user_id": subscription.user_id,
                          "merchant": subscription.merchant, "status": subscription.status})
    for product_record in gateway.wealth_products():
        product = product_record.payload
        if product.product_id in references.get("product", set()):
            facts.append({"source": "gateway", "record_id": product_record.record_id,
                          "content_hash": product_record.content_hash,
                          "product_id": product.product_id,
                          "name": product.name, "risk_level": product.risk_level,
                          "min_amount_cents": product.min_amount.cents})
    for hold_record in gateway.active_holds_of(user_id):
        hold = hold_record.payload
        if hold.hold_id in references.get("hold", set()):
            facts.append({"source": "gateway", "record_id": hold_record.record_id,
                          "content_hash": hold_record.content_hash, "hold_id": hold.hold_id,
                          "account_id": hold.account_id, "amount_cents": hold.amount.cents,
                          "status": hold.status})
    return facts


def node_review_context(
    plan: Plan, facts: Sequence[Mapping[str, object]], intents: Sequence[Mapping[str, object]],
    proposals: Mapping[str, Mapping[str, object]],
    *, review_kind: str = "user_request",
) -> dict[str, dict[str, object]]:
    """节点只引用共享事实 ID；同动作多节点的意图列为候选，不能假称一一绑定。"""
    contexts: dict[str, dict[str, object]] = {}
    fact_ids = {fact["record_id"] for fact in facts}
    for node in plan.nodes:
        proposal = proposals.get(node.node_id, {})
        refs = list(cast(list[str], proposal.get("fact_refs", [])))
        missing = list(cast(list[str], proposal.get("missing_sources", [])))
        for slot in _OBJECT_SLOTS:
            param = SLOT_PARAMS[slot]
            value = node.params.get(param)
            if value is None:
                continue
            # payer 与 payee 共用收款名册，不存在独立 payer 表。
            record_id = f"{_OBJECT_TABLES[slot]}:{value}"
            if record_id in fact_ids:
                refs.append(record_id)
            else:
                missing.append(param)
        contexts[node.node_id] = {
            "action": node.action, "authorization": "pending_confirmation",
            "intent_scope": (
                "check_due_proposal" if review_kind == "due_proposal" else "user_request"
            ),
            "proposal_basis": proposal.get("proposal_basis", {
                "source": "accepted_intents",
                "candidate_intent_indices": [
                    index for index, intent in enumerate(intents)
                    if intent.get("action") == node.action
                ],
            }),
            "fact_refs": list(dict.fromkeys(refs)),
            "missing_sources": missing,
        }
    return contexts


def bills_review_context(
    gateway: FactGateway, user_id: str, accepted: Sequence[Intent],
    intents: Sequence[Mapping[str, object]], plan: Plan,
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    """按实际采纳的可信意图独立回读账单；计划参数不能为自己提供证据。"""
    actions = {intent.action for intent in accepted}
    if not actions & {"bills.monthly", "bills.category_trend"}:
        return [], {}
    year, month = _period(accepted)
    record = gateway.monthly_bill(user_id, year, month)
    summary = record.payload
    fact: dict[str, object] = {
        "source": "gateway", "record_id": record.record_id,
        "content_hash": record.content_hash, "user_id": summary.user_id,
        "year": summary.year, "month": summary.month,
        "total_expense_cents": summary.total_expense.cents,
        "total_income_cents": summary.total_income.cents,
        "aggregation": "transactions.user_id + booked_on month; category net totals",
        "by_category": [
            {"category": item.category, "net_cents": item.total.cents}
            for item in summary.by_category
        ],
    }

    def selected_source(index: int, slot: str) -> object:
        return cast(Mapping[str, object], intents[index]["selected_sources"])[slot]

    period: dict[str, object] = {
        "year": year, "month": month, "source": "server_rule",
        "rule": "previous_complete_month",
        "reference": "gateway.core_bank.DEFAULT_BOOKING_DATE",
        "reference_date": DEFAULT_BOOKING_DATE.isoformat(),
    }
    for index, intent in enumerate(accepted):
        if intent.action in BILLS_ACTIONS and {"year", "month"} <= intent.slots.keys():
            period = {
                "year": year, "month": month, "source": "accepted_intents",
                "candidate_intent_indices": [index],
                "selected_sources": {
                    slot: selected_source(index, slot) for slot in ("year", "month")
                },
            }
            break
    trend_nodes = [
        node for node in plan.nodes
        if node.action == "bills.category_trend" and "bills.category_trend" in actions
    ]
    proposals: dict[str, dict[str, object]] = {}
    for node in plan.nodes:
        if node.action != "bills.monthly" and node not in trend_nodes:
            continue
        candidates = [
            index for index, intent in enumerate(accepted) if intent.action == node.action
        ]
        basis: dict[str, object] = {
            "source": "accepted_intents", "candidate_intent_indices": candidates,
            "period": period, "user_id": {"value": user_id, "source": "server_identity"},
        }
        if node.action == "bills.monthly" and not candidates:
            basis.update({
                "source": "server_rule",
                "required_by": [
                    trend.node_id for trend in trend_nodes if node.node_id in trend.depends_on
                ],
            })
        if node in trend_nodes:
            category: dict[str, object] = {
                "value": _chosen_category(accepted, summary), "source": "server_rule",
                "fact_ref": record.record_id,
                "rule": "largest_negative_category_net; ties use max(category)",
            }
            for index, intent in enumerate(accepted):
                if intent.action in BILLS_ACTIONS and "category" in intent.slots:
                    category = {
                        "value": category["value"], "source": selected_source(index, "category"),
                        "candidate_intent_indices": [index],
                    }
                    break
            base_year, base_month = _previous_month(year, month)
            basis.update({
                "category": category,
                "base_period": {
                    "year": base_year, "month": base_month, "source": "server_rule",
                    "rule": "previous_month_of_period",
                },
            })
        proposals[node.node_id] = {"proposal_basis": basis, "fact_refs": [record.record_id]}
    return [fact], proposals
