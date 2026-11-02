"""Customer-facing, server-derived transaction draft payloads.

Drafts are display and edit contracts only.  They are built from a frozen plan;
the patch route must regenerate that plan through ``Session`` before anything
can be confirmed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ai_banking_copilot.contracts.plan import Plan
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.web.plan_card import PlanCard


@dataclass(frozen=True)
class TransactionDraft:
    draft_id: str
    kind: str
    fields: tuple[dict[str, object], ...]
    disclosures: tuple[str, ...]
    risk_level: str
    editable_fields: frozenset[str]
    status: str
    origin_plan_hash: str

    def payload(self) -> dict[str, object]:
        return {
            "draft_id": self.draft_id,
            "kind": self.kind,
            "fields": list(self.fields),
            "disclosures": list(self.disclosures),
            "risk_level": self.risk_level,
            "editable_fields": sorted(self.editable_fields),
            "status": self.status,
            "origin_plan_hash": self.origin_plan_hash,
        }


def editable_fields_for_plan(plan: Plan) -> frozenset[str]:
    actions = {node.action for node in plan.nodes}
    if any(action.startswith("transfer.") for action in actions):
        return frozenset({"payee", "account", "amount", "transfer_method", "remark", "purpose"})
    if any(action.startswith("wealth.") for action in actions):
        return frozenset({"account", "product", "amount"})
    if "subscription.cancel" in actions:
        return frozenset({"subscription"})
    if any(action.startswith("card.") for action in actions):
        return frozenset(
            {
                "card",
                "amount",
                "daily_limit",
                "address",
                "mail_method",
                "replace",
                "loss_type",
                "loss_reason",
            }
        )
    if "merchant.order" in actions or "funds.freeze" in actions:
        return frozenset({"budget_limit", "order_amount", "order_on", "reminder_lead"})
    return frozenset()


def draft_for_plan(
    plan: Plan | None,
    card: PlanCard | None,
    *,
    origin_plan_hash: str | None = None,
) -> TransactionDraft | None:
    if plan is None or card is None or not editable_fields_for_plan(plan):
        return None
    actions = {node.action for node in plan.nodes}
    origin = origin_plan_hash or card.frozen_hash
    if any(action.startswith("transfer.") for action in actions):
        return _transfer_draft(plan, card, origin)
    if any(action.startswith("wealth.") for action in actions):
        return _wealth_draft(plan, card, origin)
    if "subscription.cancel" in actions:
        return _subscription_draft(plan, card, origin)
    if any(action.startswith("card.") for action in actions):
        return _card_draft(plan, card, origin)
    return _life_event_draft(plan, card, origin)


def _node(plan: Plan, prefix: str) -> Mapping[str, object]:
    return next((node.params for node in plan.nodes if node.action.startswith(prefix)), {})


def _yuan(value: object) -> str:
    return str(Money(value).yuan) if isinstance(value, int) and not isinstance(value, bool) else ""


def _summary_value(card: PlanCard, label: str) -> str:
    return next((line.value for line in card.summary if line.label == label), "")


def _field(
    key: str,
    label: str,
    value: object,
    *,
    display: str | None = None,
    editable: bool = True,
) -> dict[str, object]:
    raw = str(value or "")
    return {
        "key": key,
        "label": label,
        "value": raw,
        "display": display or raw,
        "editable": editable,
    }


def _draft(
    *,
    kind: str,
    card: PlanCard,
    origin: str,
    fields: tuple[dict[str, object], ...],
    disclosures: tuple[str, ...],
    plan: Plan,
) -> TransactionDraft:
    return TransactionDraft(
        draft_id=card.card_id,
        kind=kind,
        fields=fields,
        disclosures=disclosures,
        risk_level=card.level.name,
        editable_fields=editable_fields_for_plan(plan),
        status="awaiting_customer_review",
        origin_plan_hash=origin,
    )


def _transfer_draft(plan: Plan, card: PlanCard, origin: str) -> TransactionDraft:
    params = _node(plan, "transfer.")
    return _draft(
        kind="transfer",
        card=card,
        origin=origin,
        plan=plan,
        fields=(
            _field(
                "payee",
                "收款人",
                params.get("payee_id"),
                display=_summary_value(card, "对象"),
                editable=False,
            ),
            _field(
                "account",
                "付款账户",
                params.get("account_id"),
                display=_summary_value(card, "付款账户"),
                editable=False,
            ),
            _field("amount", "转账金额（元）", _yuan(params.get("amount_cents"))),
            _field(
                "transfer_method",
                "到账方式",
                params.get("transfer_method") or "realtime",
                display=_summary_value(card, "转账方式"),
            ),
            _field("remark", "附言", params.get("remark")),
            _field("purpose", "用途", params.get("purpose")),
        ),
        disclosures=(
            "收款人、金额和到账方式以重新冻结后的计划为准。",
            "修改后原确认凭证立即失效，需重新核对。",
        ),
    )


def _wealth_draft(plan: Plan, card: PlanCard, origin: str) -> TransactionDraft:
    params = _node(plan, "wealth.")
    return _draft(
        kind="wealth",
        card=card,
        origin=origin,
        plan=plan,
        fields=(
            _field(
                "product",
                "理财产品",
                params.get("product_id"),
                display=_summary_value(card, "对象"),
                editable=False,
            ),
            _field("account", "交易账户", params.get("account_id"), editable=False),
            _field("amount", "交易金额（元）", _yuan(params.get("amount_cents"))),
        ),
        disclosures=(
            "产品收益不保证，申购前须完成适当性与风险揭示。",
            "申购属于红色操作，确认后仍需服务端强验证。",
        ),
    )


def _subscription_draft(plan: Plan, card: PlanCard, origin: str) -> TransactionDraft:
    params = _node(plan, "subscription.")
    return _draft(
        kind="subscription",
        card=card,
        origin=origin,
        plan=plan,
        fields=(
            _field(
                "subscription",
                "扣款授权",
                params.get("subscription_id"),
                display=_summary_value(card, "对象"),
                editable=False,
            ),
        ),
        disclosures=(
            "本操作仅终止本行侧扣款授权。",
            "商户会员或服务是否取消，以商户侧处理结果为准。",
        ),
    )


def _card_draft(plan: Plan, card: PlanCard, origin: str) -> TransactionDraft:
    params = _node(plan, "card.")
    fields = (
        _field(
            "card",
            "卡片",
            params.get("card_id"),
            display=_summary_value(card, "对象"),
            editable=False,
        ),
        _field("amount", "额度（元）", _yuan(params.get("amount_cents"))),
        _field("daily_limit", "日限额（元）", _yuan(params.get("daily_limit_cents"))),
        _field("address", "寄送地址", params.get("address")),
        _field("loss_reason", "原因", params.get("reason")),
    )
    return _draft(
        kind="card",
        card=card,
        origin=origin,
        plan=plan,
        fields=fields,
        disclosures=(
            "挂失、补卡和额度调整按重新冻结后的计划办理。",
            "补卡寄送与费用为演示数据，不代表真实制卡或邮寄承诺。",
        ),
    )


def _life_event_draft(plan: Plan, card: PlanCard, origin: str) -> TransactionDraft:
    freeze = next((node.params for node in plan.nodes if node.action == "funds.freeze"), {})
    order = next((node.params for node in plan.nodes if node.action == "merchant.order"), {})
    return _draft(
        kind="life_event",
        card=card,
        origin=origin,
        plan=plan,
        fields=(
            _field("budget_limit", "预算上限（元）", _yuan(freeze.get("amount_cents"))),
            _field("order_amount", "订单金额（元）", _yuan(order.get("amount_cents"))),
            _field("order_on", "下单日期", order.get("order_on")),
        ),
        disclosures=(
            "预算上限与订单金额分别展示，预算不会自动成为订单金额。",
            "商户下单为演示模拟，不代表真实商户订单。",
        ),
    )
