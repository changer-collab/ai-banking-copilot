"""Constrained semantic proposals for banking requests.

The model may name a customer-facing semantic triple, but the server alone owns
the mapping from that triple to a registered action.  Frames never carry an
object identifier, policy decision, or verification result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

Domain = Literal["account", "transfer", "bills", "wealth", "card", "subscription", "life_event"]
Mode = Literal["query", "write", "schedule"]
Role = Literal[
    "transfer_amount",
    "investment_amount",
    "redemption_shares",
    "budget_limit",
    "order_amount",
    "daily_limit",
    "merchant",
    "payee_hint",
    "product_hint",
    "schedule_time",
    "reminder_lead",
    "purpose",
    "privacy_constraint",
    "credit_limit",
    "cycle_limit",
    "lock_duration",
    "delivery_address",
    "loss_reason",
]


class FrameError(ValueError):
    """A frame cannot safely be used as a planning hint."""


@dataclass(frozen=True)
class SemanticValue:
    value: str | int | bool
    role: Role
    evidence: str


@dataclass(frozen=True)
class SemanticFrame:
    domain: Domain
    operation: str
    mode: Mode
    evidence: str
    confidence: float
    roles: Mapping[str, SemanticValue] = field(default_factory=dict)


@dataclass(frozen=True)
class FrameProposal:
    frames: tuple[SemanticFrame, ...]
    raw: str
    channel: str | None = None


@dataclass(frozen=True)
class FrameOption:
    """A server-owned semantic triple and its one registered action."""

    domain: Domain
    operation: str
    mode: Mode
    action: str
    allowed_roles: frozenset[str] = frozenset()


_FRAME_OPTIONS: tuple[FrameOption, ...] = (
    FrameOption("account", "balance", "query", "account.balance"),
    FrameOption("account", "transactions", "query", "account.transactions"),
    FrameOption(
        "transfer",
        "create",
        "write",
        "transfer.create",
        frozenset({"transfer_amount", "payee_hint", "purpose", "privacy_constraint"}),
    ),
    FrameOption(
        "transfer",
        "schedule",
        "schedule",
        "transfer.schedule",
        frozenset(
            {
                "transfer_amount",
                "payee_hint",
                "schedule_time",
                "purpose",
                "privacy_constraint",
            }
        ),
    ),
    FrameOption(
        "transfer",
        "book",
        "schedule",
        "transfer.book",
        frozenset(
            {
                "transfer_amount",
                "payee_hint",
                "schedule_time",
                "purpose",
                "privacy_constraint",
            }
        ),
    ),
    FrameOption(
        "transfer",
        "request_collection",
        "write",
        "transfer.request_collection",
        frozenset({"transfer_amount", "payee_hint", "purpose"}),
    ),
    FrameOption("bills", "monthly", "query", "bills.monthly"),
    FrameOption("bills", "category_trend", "query", "bills.category_trend"),
    FrameOption("bills", "yearly", "query", "bills.yearly"),
    FrameOption("wealth", "list", "query", "wealth.list", frozenset({"product_hint"})),
    FrameOption(
        "wealth",
        "subscribe",
        "write",
        "wealth.subscribe",
        frozenset({"investment_amount", "product_hint"}),
    ),
    FrameOption(
        "wealth",
        "redeem",
        "write",
        "wealth.redeem",
        frozenset({"investment_amount", "redemption_shares", "product_hint"}),
    ),
    FrameOption("card", "list", "query", "card.list"),
    FrameOption("card", "lock", "write", "card.lock", frozenset({"lock_duration", "loss_reason"})),
    FrameOption("card", "unlock", "write", "card.unlock"),
    FrameOption("card", "report_lost", "write", "card.report_lost", frozenset({"loss_reason"})),
    FrameOption(
        "card",
        "replace",
        "write",
        "card.replace",
        frozenset({"delivery_address", "loss_reason"}),
    ),
    FrameOption(
        "card",
        "adjust_credit_limit",
        "write",
        "card.adjust_credit_limit",
        frozenset({"credit_limit"}),
    ),
    FrameOption(
        "card",
        "set_cycle_limit",
        "write",
        "card.set_cycle_limit",
        frozenset({"cycle_limit"}),
    ),
    FrameOption("card", "apply_virtual", "write", "card.apply_virtual", frozenset({"daily_limit"})),
    FrameOption("card", "set_limit", "write", "card.set_limit", frozenset({"daily_limit"})),
    FrameOption("card", "change_password", "write", "password.change"),
    FrameOption("subscription", "list", "query", "subscription.list"),
    FrameOption("subscription", "cancel", "write", "subscription.cancel", frozenset({"merchant"})),
    FrameOption(
        "subscription",
        "sign",
        "write",
        "subscription.sign",
        frozenset({"merchant", "transfer_amount"}),
    ),
    FrameOption(
        "life_event",
        "freeze_funds",
        "write",
        "funds.freeze",
        frozenset({"budget_limit", "purpose"}),
    ),
    FrameOption("life_event", "release_funds", "write", "funds.release"),
    FrameOption(
        "life_event",
        "birthday_plan",
        "schedule",
        "merchant.order",
        frozenset({"budget_limit", "order_amount", "merchant", "schedule_time", "reminder_lead"}),
    ),
)

_ACTION_BY_FRAME: Mapping[tuple[str, str, str], str] = {
    (option.domain, option.operation, option.mode): option.action for option in _FRAME_OPTIONS
}
_ALLOWED_ROLES: Mapping[tuple[str, str, str], frozenset[str]] = {
    (option.domain, option.operation, option.mode): option.allowed_roles
    for option in _FRAME_OPTIONS
}


def frame_options_for_actions(actions: Sequence[str]) -> tuple[FrameOption, ...]:
    """Return prompt-safe semantic choices for the selected registered actions."""
    allowed = frozenset(actions)
    return tuple(option for option in _FRAME_OPTIONS if option.action in allowed)


def action_for_frame(frame: SemanticFrame) -> str | None:
    """Map a semantic category to a server-owned action name."""
    return _ACTION_BY_FRAME.get((frame.domain, frame.operation, frame.mode))


def normalize_frame(frame: SemanticFrame) -> SemanticFrame:
    """Apply narrow, customer-wording disambiguation before action mapping.

    ``transfer.schedule`` creates an active timed transfer.  ``transfer.book``
    only records a later appointment.  Models commonly conflate the two, but
    the Chinese verb ``定时`` is unambiguous unless it is explicitly prefixed
    by ``登记``.  This normalization remains semantic-only and never supplies
    an amount, payee, date, or authorization fact.
    """
    if (
        frame.domain == "transfer"
        and frame.operation == "book"
        and frame.mode == "schedule"
        and "登记" not in frame.evidence
        and any(marker in frame.evidence for marker in ("定时", "定转", "预约转"))
    ):
        return replace(frame, operation="schedule")
    return frame


_FORBIDDEN_ROLE_NAMES = frozenset(
    {
        "payee_id",
        "account_id",
        "card_id",
        "product_id",
        "subscription_id",
        "user_id",
        "level",
        "decision",
        "authorization",
        "verification",
    }
)


def validate_frame(frame: SemanticFrame, utterance: str) -> SemanticFrame:
    """Fail closed when the proposal exceeds its semantic-only contract."""
    if not 0.0 <= frame.confidence <= 1.0:
        raise FrameError("confidence must be between 0 and 1")
    if not frame.evidence or frame.evidence not in utterance:
        raise FrameError("frame evidence must be a continuous user-utterance substring")
    allowed = _ALLOWED_ROLES.get((frame.domain, frame.operation, frame.mode), frozenset())
    for name, value in frame.roles.items():
        if name in _FORBIDDEN_ROLE_NAMES:
            raise FrameError(f"forbidden role: {name}")
        if value.role not in allowed:
            raise FrameError(f"role {value.role} is not allowed for this frame")
        if not value.evidence or value.evidence not in utterance:
            raise FrameError("role evidence must be a continuous user-utterance substring")
    return frame
