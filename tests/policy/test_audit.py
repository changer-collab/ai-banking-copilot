from datetime import date
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.policy.audit import (
    GENESIS_HASH,
    KIND_DECISION,
    KIND_EXECUTION,
    AuditEvent,
    AuditLog,
    ChainBrokenError,
)


def _event(event_id: str, kind: str = KIND_EXECUTION, prev_hash: str = "") -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        kind=kind,
        payload={"action": "transfer.create", "level": "RED"},
        prev_hash=prev_hash,
    )


def test_empty_log_has_genesis_head_and_valid_chain(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    assert log.head() == GENESIS_HASH
    assert log.events() == ()
    assert log.verify_chain() is True


def test_append_links_chain_and_returns_new_head(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    first = log.append(_event("e1"))
    second = log.append(_event("e2"))
    assert first != second
    assert log.head() == second
    events = log.events()
    assert [event.event_id for event in events] == ["e1", "e2"]
    assert events[0].prev_hash == GENESIS_HASH
    assert events[1].prev_hash == first
    assert log.verify_chain() is True


def test_append_accepts_explicit_correct_prev_hash(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    first = log.append(_event("e1"))
    log.append(_event("e2", prev_hash=first))
    assert log.verify_chain() is True


def test_append_rejects_stale_prev_hash(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(_event("e1"))
    with pytest.raises(ChainBrokenError, match="断裂"):
        log.append(_event("e2", prev_hash="0" * 63 + "1"))


def test_tampered_payload_breaks_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(_event("e1"))
    log.append(_event("e2"))
    path.write_text(
        path.read_text(encoding="utf-8").replace('"RED"', '"GREEN"'), encoding="utf-8"
    )
    assert log.verify_chain() is False


def test_malformed_line_breaks_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(_event("e1"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
    assert log.verify_chain() is False


def test_payload_is_canonicalized_on_round_trip(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(
        AuditEvent(
            event_id="e1",
            kind=KIND_DECISION,
            payload={"amount": Money(1234), "booked_on": date(2026, 10, 1)},
            prev_hash="",
        )
    )
    payload = log.events()[0].payload
    assert payload["amount"] == {"cents": 1234}
    assert payload["booked_on"] == "2026-10-01"
    assert log.verify_chain() is True


def test_log_is_append_only_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(_event("e1"))
    log.append(_event("e2"))
    log.append(_event("e3"))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3


def test_event_requires_id_and_kind() -> None:
    with pytest.raises(ValueError, match="event_id"):
        AuditEvent(event_id="", kind=KIND_EXECUTION, payload={}, prev_hash="")
    with pytest.raises(ValueError, match="kind"):
        AuditEvent(event_id="e1", kind="", payload={}, prev_hash="")
