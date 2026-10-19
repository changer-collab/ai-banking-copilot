"""Reviewer 使用的最小网关出处摘要；只回读已采纳控制槽引用的对象。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ai_banking_copilot.gateway.facts import FactGateway


def referenced_facts(
    gateway: FactGateway, user_id: str, intents: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    references: dict[str, set[str]] = {}
    for intent in intents:
        slots = intent.get("slots")
        if not isinstance(slots, dict):
            continue
        for slot in ("account", "payee", "payer", "card", "subscription", "product", "hold"):
            value = slots.get(slot)
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
