from __future__ import annotations

from dataclasses import dataclass

import pytest

from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.web.channels.ratelimit import (
    EventLedger,
    HealthRegistry,
    TokenBucketRateLimiter,
    retry_outbound,
)


@dataclass
class _Response:
    status_code: int


class _Sender:
    def __init__(self, failures: int, status_code: int = 503) -> None:
        self.failures = failures
        self.status_code = status_code
        self.calls = 0

    def send(self) -> _Response:
        self.calls += 1
        if self.calls <= self.failures:
            return _Response(self.status_code)
        return _Response(200)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_token_bucket_refills_and_isolates_external_identities() -> None:
    clock = _Clock()
    limiter = TokenBucketRateLimiter(limit=20, window_s=60, clock=clock)

    assert [limiter.allow("feishu:ou-a") for _ in range(25)] == [True] * 20 + [
        False
    ] * 5
    assert [limiter.allow("feishu:ou-b") for _ in range(20)] == [True] * 20
    clock.advance(3.0)
    assert limiter.allow("feishu:ou-a") is True
    assert limiter.allow("feishu:ou-a") is False


def test_token_bucket_reservation_is_consumed_once_and_expires() -> None:
    clock = _Clock()
    limiter = TokenBucketRateLimiter(
        limit=4,
        window_s=60,
        clock=clock,
        reservation_ttl_s=60,
        max_reservations=2,
    )

    assert limiter.reserve("feishu:ou-a", "evt-1") is True
    assert limiter.allow("feishu:ou-a", reservation_id="evt-1") is True
    assert limiter.reserve("feishu:ou-a", "evt-2") is True
    assert limiter.reserve("feishu:ou-a", "evt-3") is True
    assert limiter.reserve("feishu:ou-a", "evt-4") is False
    clock.advance(60.0)
    assert limiter.allow("feishu:ou-a", reservation_id="evt-1") is True


def test_event_id_is_claimed_once_across_ledger_instances(tmp_path) -> None:
    path = tmp_path / "channel.db"
    writer = Store(path)
    init_schema(writer)
    first = EventLedger(writer).claim("evt-1")
    second = EventLedger(writer).claim("evt-1")
    writer.close()

    reader = Store(path, StoreMode.READ_WRITE)
    third = EventLedger(reader).claim("evt-1")
    reader.close()

    assert first is True
    assert second is False
    assert third is False


def test_retry_outbound_tries_four_times_for_5xx() -> None:
    sender = _Sender(failures=3)
    sleeps: list[float] = []

    result = retry_outbound(sender.send, sleeper=sleeps.append)

    assert result.status_code == 200
    assert sender.calls == 4
    assert sleeps == [0.5, 2.0, 8.0]


def test_retry_outbound_does_not_retry_4xx() -> None:
    sender = _Sender(failures=1, status_code=400)

    result = retry_outbound(sender.send, sleeper=lambda _delay: None)

    assert result.status_code == 400
    assert sender.calls == 1


def test_retry_outbound_reports_only_scheduled_retries() -> None:
    sender = _Sender(failures=3)
    attempts: list[tuple[int, int | None, str | None]] = []
    retries: list[tuple[int, float]] = []

    result = retry_outbound(
        sender.send,
        sleeper=lambda _delay: None,
        on_attempt=lambda no, status, error: attempts.append((no, status, error)),
        on_retry=lambda attempt_no, delay: retries.append((attempt_no, delay)),
    )

    assert result.status_code == 200
    assert attempts == [
        (1, 503, None),
        (2, 503, None),
        (3, 503, None),
        (4, 200, None),
    ]
    assert retries == [(1, 0.5), (2, 2.0), (3, 8.0)]


def test_retry_outbound_retries_timeout_and_reports_only_error_type() -> None:
    calls = 0
    attempts: list[tuple[int, int | None, str | None]] = []

    def send() -> _Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("secret response body")
        return _Response(200)

    result = retry_outbound(
        send,
        sleeper=lambda _delay: None,
        on_attempt=lambda no, status, error: attempts.append((no, status, error)),
    )

    assert result.status_code == 200
    assert attempts == [(1, None, "TimeoutError"), (2, 200, None)]
    assert "secret response body" not in repr(attempts)


def test_retry_outbound_does_not_report_retry_after_final_5xx_or_4xx() -> None:
    server_error = _Sender(failures=4)
    server_retries: list[tuple[int, float]] = []
    server_attempts: list[tuple[int, int | None, str | None]] = []
    client_error = _Sender(failures=1, status_code=400)
    client_retries: list[tuple[int, float]] = []
    client_attempts: list[tuple[int, int | None, str | None]] = []

    server_result = retry_outbound(
        server_error.send,
        sleeper=lambda _delay: None,
        on_attempt=lambda no, status, error: server_attempts.append(
            (no, status, error)
        ),
        on_retry=lambda attempt_no, delay: server_retries.append((attempt_no, delay)),
    )
    client_result = retry_outbound(
        client_error.send,
        sleeper=lambda _delay: None,
        on_attempt=lambda no, status, error: client_attempts.append(
            (no, status, error)
        ),
        on_retry=lambda attempt_no, delay: client_retries.append((attempt_no, delay)),
    )

    assert server_result.status_code == 503
    assert server_attempts == [(index, 503, None) for index in range(1, 5)]
    assert server_retries == [(1, 0.5), (2, 2.0), (3, 8.0)]
    assert client_result.status_code == 400
    assert client_attempts == [(1, 400, None)]
    assert client_retries == []


def test_retry_outbound_reports_final_timeout_before_raising() -> None:
    attempts: list[tuple[int, int | None, str | None]] = []

    def timeout() -> _Response:
        raise TimeoutError("private upstream detail")

    with pytest.raises(TimeoutError, match="private upstream detail"):
        retry_outbound(
            timeout,
            sleeper=lambda _delay: None,
            on_attempt=lambda no, status, error: attempts.append((no, status, error)),
        )

    assert attempts == [
        (1, None, "TimeoutError"),
        (2, None, "TimeoutError"),
        (3, None, "TimeoutError"),
        (4, None, "TimeoutError"),
    ]


def test_health_registry_reports_events_and_rate_limits() -> None:
    health = HealthRegistry()
    health.register("feishu", replay_mode=True)
    health.record_event("feishu", success=True)
    health.record_event("feishu", success=False)
    health.record_replay("feishu")
    health.record_retry("feishu")
    health.record_rate_limit("feishu")

    snapshot = health.snapshot()

    assert snapshot["feishu"]["events"] == 2
    assert snapshot["feishu"]["ok"] == 1
    assert snapshot["feishu"]["failed"] == 1
    assert snapshot["feishu"]["replayed"] == 1
    assert snapshot["feishu"]["retries"] == 1
    assert snapshot["feishu"]["ratelimit_hits"] == 1
    assert snapshot["feishu"]["replay_mode"] is True
