"""客户级日限额与官方口径的关系：**只能加严、不能放宽**。"""

from __future__ import annotations

from ai_banking_copilot.contracts.authorization import Level, ReasonCode
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.policy.pdp import ObjectState, PolicyRequest, evaluate


def _request(accumulated_cents: int, limit_cents: int | None) -> PolicyRequest:
    return PolicyRequest(
        user_id="u1",
        action="transfer.create",
        amount=Money(accumulated_cents),
        daily_accumulated=Money(accumulated_cents),
        object_state=ObjectState.VERIFIED_SAVED,
        is_pure_query=False,
        irreversible=False,
        daily_limit=None if limit_cents is None else Money(limit_cents),
    )


def test_customer_limit_tightens_the_band() -> None:
    decision = evaluate(_request(60_000, 80_000))
    assert decision.level is Level.YELLOW


def test_customer_limit_at_the_band_edge_is_yellow() -> None:
    decision = evaluate(_request(80_000, 80_000))
    assert decision.level is Level.YELLOW


def test_customer_limit_beyond_edge_is_red() -> None:
    decision = evaluate(_request(90_000, 80_000))
    assert decision.level is Level.RED
    assert ReasonCode.AMOUNT_OVER_DAILY_LIMIT in decision.reasons


def test_customer_limit_can_never_loosen_the_official_cap() -> None:
    decision = evaluate(_request(150_000, 500_000))
    assert decision.level is Level.RED
    assert ReasonCode.AMOUNT_OVER_DAILY_LIMIT in decision.reasons


def test_official_default_applies_without_customer_limit() -> None:
    assert evaluate(_request(90_000, None)).level is Level.YELLOW
    assert evaluate(_request(110_000, None)).level is Level.RED
