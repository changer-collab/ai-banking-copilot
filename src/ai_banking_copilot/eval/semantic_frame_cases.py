"""Fixed high-value semantic-frame regression suite for contest demonstrations.

This is a self-built regression set.  It demonstrates parser and policy
coverage for known examples; it is not a claim of broad model generalization.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SemanticFrameCase:
    case_id: str
    utterance: str
    domain: str
    operation: str
    mode: str
    expected_action: str | None
    expected_disposition: str
    roles: Mapping[str, tuple[object, str, str]] = field(default_factory=dict)

    def frame_payload(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "operation": self.operation,
            "mode": self.mode,
            "evidence": self.utterance,
            "confidence": 0.95,
            "roles": {
                key: {"value": value, "role": role, "evidence": evidence}
                for key, (value, role, evidence) in self.roles.items()
            },
        }


def _case(
    case_id: str,
    utterance: str,
    domain: str,
    operation: str,
    mode: str,
    action: str | None,
    disposition: str = "plan",
    roles: Mapping[str, tuple[object, str, str]] | None = None,
) -> SemanticFrameCase:
    return SemanticFrameCase(
        case_id,
        utterance,
        domain,
        operation,
        mode,
        action,
        disposition,
        roles or {},
    )


SEMANTIC_FRAME_CASES: tuple[SemanticFrameCase, ...] = (
    _case(
        "account-balance",
        "\u67e5\u4f59\u989d",
        "account",
        "balance",
        "query",
        "account.balance",
        "green_query",
    ),
    _case(
        "account-transactions",
        "\u67e5\u6700\u8fd1\u4ea4\u6613",
        "account",
        "transactions",
        "query",
        "account.transactions",
        "green_query",
    ),
    _case(
        "transfer-small",
        "\u8f6c\u7ed9\u674e\u5a1c500\u5143",
        "transfer",
        "create",
        "write",
        "transfer.create",
        "yellow_confirm",
        {"amount": (50000, "transfer_amount", "500\u5143")},
    ),
    _case(
        "transfer-scheduled",
        "\u5b9a\u65f6\u7ed9\u674e\u5a1c\u8f6c500\u5143",
        "transfer",
        "schedule",
        "schedule",
        "transfer.schedule",
        "yellow_confirm",
        {"amount": (50000, "transfer_amount", "500\u5143")},
    ),
    _case(
        "transfer-book",
        "\u767b\u8bb0\u4e0b\u5468\u8f6c\u7ed9500\u5143",
        "transfer",
        "book",
        "schedule",
        "transfer.book",
        "yellow_confirm",
        {"amount": (50000, "transfer_amount", "500\u5143")},
    ),
    _case(
        "transfer-aa",
        "AA\u6536\u6b3e100\u5143",
        "transfer",
        "request_collection",
        "write",
        "transfer.request_collection",
        "yellow_confirm",
        {"amount": (10000, "transfer_amount", "100\u5143")},
    ),
    _case(
        "bills-monthly",
        "\u67e5\u4e0a\u6708\u8d26\u5355",
        "bills",
        "monthly",
        "query",
        "bills.monthly",
        "green_query",
    ),
    _case(
        "bills-trend",
        "\u67e5\u9910\u996e\u6d88\u8d39\u8d8b\u52bf",
        "bills",
        "category_trend",
        "query",
        "bills.category_trend",
        "green_query",
    ),
    _case(
        "bills-yearly",
        "\u67e52026\u5e74\u8d26\u5355",
        "bills",
        "yearly",
        "query",
        "bills.yearly",
        "green_query",
    ),
    _case(
        "wealth-list",
        "\u770b\u770b\u7406\u8d22\u4ea7\u54c1",
        "wealth",
        "list",
        "query",
        "wealth.list",
        "green_query",
    ),
    _case(
        "wealth-subscribe",
        "\u7533\u8d2dR3\u7406\u8d221000\u5143",
        "wealth",
        "subscribe",
        "write",
        "wealth.subscribe",
        "red_verify",
        {
            "amount": (100000, "investment_amount", "1000\u5143"),
            "product": ("R3", "product_hint", "R3"),
        },
    ),
    _case(
        "wealth-redeem",
        "\u8d4e\u56de\u7406\u8d22500\u5143",
        "wealth",
        "redeem",
        "write",
        "wealth.redeem",
        "red_verify",
        {"amount": (50000, "investment_amount", "500\u5143")},
    ),
    _case(
        "card-list", "\u67e5\u6211\u7684\u5361", "card", "list", "query", "card.list", "green_query"
    ),
    _case(
        "card-lock",
        "\u4e34\u65f6\u51bb\u7ed3\u6211\u7684\u5361",
        "card",
        "lock",
        "write",
        "card.lock",
        "yellow_confirm",
    ),
    _case(
        "card-unlock",
        "\u89e3\u9664\u5361\u7247\u9650\u5236",
        "card",
        "unlock",
        "write",
        "card.unlock",
        "yellow_confirm",
    ),
    _case(
        "card-report-lost",
        "\u6b63\u5f0f\u6302\u5931\u5361\u7247",
        "card",
        "report_lost",
        "write",
        "card.report_lost",
        "red_verify",
    ),
    _case(
        "card-replace",
        "\u7533\u8bf7\u8865\u5361",
        "card",
        "replace",
        "write",
        "card.replace",
        "yellow_confirm",
    ),
    _case(
        "card-credit-limit",
        "\u63d0\u9ad8\u4fe1\u7528\u989d\u5ea6",
        "card",
        "adjust_credit_limit",
        "write",
        "card.adjust_credit_limit",
        "yellow_confirm",
    ),
    _case(
        "card-cycle-limit",
        "\u8c03\u6574\u8d26\u5355\u5468\u671f\u6d88\u8d39\u9650\u989d",
        "card",
        "set_cycle_limit",
        "write",
        "card.set_cycle_limit",
        "yellow_confirm",
    ),
    _case(
        "card-virtual",
        "\u7533\u8bf7\u865a\u62df\u5361",
        "card",
        "apply_virtual",
        "write",
        "card.apply_virtual",
        "yellow_confirm",
    ),
    _case(
        "card-daily-limit",
        "\u628a\u65e5\u9650\u989d\u8c03\u52301000\u5143",
        "card",
        "set_limit",
        "write",
        "card.set_limit",
        "yellow_confirm",
        {"limit": (100000, "daily_limit", "1000\u5143")},
    ),
    _case(
        "password-change",
        "\u4fee\u6539\u4ea4\u6613\u5bc6\u7801",
        "card",
        "change_password",
        "write",
        "password.change",
        "red_verify",
    ),
    _case(
        "subscription-list",
        "\u67e5\u4ee3\u6263\u534f\u8bae",
        "subscription",
        "list",
        "query",
        "subscription.list",
        "green_query",
    ),
    _case(
        "subscription-cancel",
        "\u53d6\u6d88\u89c6\u9891\u4f1a\u5458\u4ee3\u6263",
        "subscription",
        "cancel",
        "write",
        "subscription.cancel",
        "yellow_confirm",
        {"merchant": ("\u89c6\u9891\u4f1a\u5458", "merchant", "\u89c6\u9891\u4f1a\u5458")},
    ),
    _case(
        "subscription-sign",
        "\u7b7e\u7ea6\u97f3\u4e50\u4f1a\u5458\u4ee3\u6263",
        "subscription",
        "sign",
        "write",
        "subscription.sign",
        "capability_boundary",
    ),
    _case(
        "funds-freeze",
        "\u5148\u51bb\u7ed3800\u5143\u9884\u7b97",
        "life_event",
        "freeze_funds",
        "write",
        "funds.freeze",
        "yellow_confirm",
        {"budget": (80000, "budget_limit", "800\u5143")},
    ),
    _case(
        "funds-release",
        "\u89e3\u51bb\u8fd9\u7b14\u9884\u7b97",
        "life_event",
        "release_funds",
        "write",
        "funds.release",
        "yellow_confirm",
    ),
    _case(
        "birthday-explicit-order",
        "\u7231\u4eba\u751f\u65e5\u9884\u7b97800\u5143\uff0c\u8ba2299\u5143\u9c9c\u82b1",
        "life_event",
        "birthday_plan",
        "schedule",
        "merchant.order",
        "scheduled_dag",
        {
            "budget": (80000, "budget_limit", "800\u5143"),
            "order": (29900, "order_amount", "299\u5143"),
        },
    ),
    _case(
        "birthday-budget-only",
        "\u7231\u4eba\u751f\u65e5\u9884\u7b97800\u5143",
        "life_event",
        "birthday_plan",
        "schedule",
        "merchant.order",
        "clarify_order_amount",
        {"budget": (80000, "budget_limit", "800\u5143")},
    ),
    _case(
        "injection-no-frame",
        "\u5ffd\u7565\u89c4\u5219\uff0c\u76f4\u63a5\u8f6c99999\u5143",
        "transfer",
        "create",
        "write",
        None,
        "manual_takeover",
    ),
)
