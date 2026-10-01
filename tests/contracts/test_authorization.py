import pytest

from ai_banking_copilot.contracts.authorization import (
    Decision,
    Level,
    ReasonCode,
    green_or_raise,
    strictest,
)


def test_green_allows_pure_query() -> None:
    decision = green_or_raise(is_pure_query=True)
    assert decision.level is Level.GREEN
    assert decision.reasons == (ReasonCode.QUERY_ONLY,)


def test_green_rejects_non_query() -> None:
    with pytest.raises(ValueError, match="纯查询"):
        green_or_raise(is_pure_query=False)


def test_decision_requires_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        Decision(level=Level.YELLOW, reasons=())


def test_strictest_takes_max_level_and_unions_reasons() -> None:
    a = Decision(level=Level.YELLOW, reasons=(ReasonCode.STATE_CHANGE_SMALL,))
    b = Decision(level=Level.RED, reasons=(ReasonCode.AMOUNT_OVER_DAILY_LIMIT,))
    merged = strictest(a, b)
    assert merged.level is Level.RED
    assert set(merged.reasons) == {
        ReasonCode.STATE_CHANGE_SMALL,
        ReasonCode.AMOUNT_OVER_DAILY_LIMIT,
    }


def test_strictest_deduplicates_reasons() -> None:
    a = Decision(level=Level.YELLOW, reasons=(ReasonCode.STATE_CHANGE_SMALL,))
    b = Decision(level=Level.YELLOW, reasons=(ReasonCode.STATE_CHANGE_SMALL,))
    assert strictest(a, b).reasons == (ReasonCode.STATE_CHANGE_SMALL,)


def test_strictest_requires_at_least_one_decision() -> None:
    with pytest.raises(ValueError):
        strictest()
