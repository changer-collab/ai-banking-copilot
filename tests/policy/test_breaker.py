import pytest

from ai_banking_copilot.policy.breaker import (
    TRIGGERS,
    BreakerState,
    BreakerTrigger,
    CircuitBreaker,
)


def test_initial_state_is_closed() -> None:
    breaker = CircuitBreaker()
    assert breaker.state() is BreakerState.CLOSED
    assert breaker.is_open() is False
    assert breaker.triggers() == ()
    assert breaker.failure_count() == 0


def test_consecutive_failures_trip_at_threshold() -> None:
    breaker = CircuitBreaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open() is False
    breaker.record_failure()
    assert breaker.is_open() is True
    assert breaker.triggers() == (BreakerTrigger.CONSECUTIVE_AUTH_FAILURES,)


def test_success_resets_counter_only_when_closed() -> None:
    breaker = CircuitBreaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.failure_count() == 0
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open() is False
    breaker.record_failure()
    assert breaker.is_open() is True


def test_open_state_never_downgrades_on_success() -> None:
    breaker = CircuitBreaker(threshold=1)
    breaker.record_failure()
    assert breaker.is_open() is True
    breaker.record_success()
    assert breaker.is_open() is True
    assert breaker.state() is BreakerState.OPEN


def test_all_five_triggers_can_trip_the_breaker() -> None:
    assert len(TRIGGERS) == 5
    for trigger in TRIGGERS:
        breaker = CircuitBreaker()
        breaker.trip(trigger)
        assert breaker.is_open() is True
        assert trigger in breaker.triggers()


def test_unlock_is_the_only_downgrade_exit() -> None:
    breaker = CircuitBreaker(threshold=1)
    breaker.record_failure()
    assert breaker.is_open() is True
    breaker.unlock()
    assert breaker.is_open() is False
    assert breaker.failure_count() == 0


def test_manual_lock_opens_without_trigger() -> None:
    breaker = CircuitBreaker()
    breaker.lock()
    assert breaker.is_open() is True
    assert breaker.triggers() == ()


def test_trip_rejects_plain_string() -> None:
    breaker = CircuitBreaker()
    with pytest.raises(TypeError, match="BreakerTrigger"):
        breaker.trip("injection_detected")  # type: ignore[arg-type]


def test_invalid_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="阈值"):
        CircuitBreaker(threshold=0)
