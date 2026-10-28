from __future__ import annotations

from dataclasses import dataclass

from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.web.channels.ratelimit import (
    EventLedger,
    HealthRegistry,
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


def test_retry_outbound_tries_three_times_for_5xx() -> None:
    sender = _Sender(failures=2)
    sleeps: list[float] = []

    result = retry_outbound(sender.send, sleeper=sleeps.append)

    assert result.status_code == 200
    assert sender.calls == 3
    assert sleeps == [0.5, 2.0]


def test_retry_outbound_does_not_retry_4xx() -> None:
    sender = _Sender(failures=1, status_code=400)

    result = retry_outbound(sender.send, sleeper=lambda _delay: None)

    assert result.status_code == 400
    assert sender.calls == 1


def test_health_registry_reports_events_and_rate_limits() -> None:
    health = HealthRegistry()
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
