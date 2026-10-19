from pathlib import Path

import pytest

from ai_banking_copilot.policy.audit import AuditEvent, AuditLog
from ai_banking_copilot.policy.audit_validation import verify_for_service


def _event(event_id: str = "event") -> AuditEvent:
    return AuditEvent(event_id, "execution", {"approved": True}, "")


def _migration(payload: dict[str, object]) -> AuditEvent:
    return AuditEvent("migration", "audit_migration", payload, "")


@pytest.mark.parametrize("key", [None, "", "  "])
def test_service_requires_a_key_even_for_an_empty_chain(tmp_path: Path, key: str | None) -> None:
    assert not verify_for_service(AuditLog(tmp_path / "audit.jsonl", key=key))


def test_service_accepts_empty_and_complete_hmac_chains(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl", key="secret")
    assert verify_for_service(audit)
    audit.append(_event())
    audit.append(_event("next"))
    assert verify_for_service(audit)


def test_service_accepts_an_explicitly_migrated_sha_prefix(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    legacy = AuditLog(path)
    legacy.append(_event())
    head = legacy.append(_event("second"))
    audit = AuditLog(path, key="secret")
    audit.append(_migration({"legacy_count": 2, "legacy_head": head}))
    audit.append(_event("new"))
    assert verify_for_service(audit)
    assert not verify_for_service(AuditLog(path, key="wrong"))


@pytest.mark.parametrize("first_hmac", ["execution", "audit_migration"])
def test_service_rejects_legacy_chains_without_the_exact_migration_anchor(
    tmp_path: Path, first_hmac: str
) -> None:
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append(_event())
    audit = AuditLog(path, key="secret")
    assert not verify_for_service(audit)
    audit.append(AuditEvent("new", first_hmac, {}, ""))
    assert audit.verify_chain()
    assert not verify_for_service(audit)


@pytest.mark.parametrize(
    "change",
    [
        {"legacy_count": 2},
        {"legacy_count": True},
        {"legacy_head": "0" * 64},
        {"extra": "unbound"},
    ],
)
def test_service_rejects_a_forged_migration_payload(
    tmp_path: Path, change: dict[str, object]
) -> None:
    path = tmp_path / "audit.jsonl"
    head = AuditLog(path).append(_event())
    payload: dict[str, object] = {"legacy_count": 1, "legacy_head": head}
    payload.update(change)
    audit = AuditLog(path, key="secret")
    audit.append(_migration(payload))
    assert audit.verify_chain()
    assert not verify_for_service(audit)


@pytest.mark.parametrize("legacy_prefix", [False, True])
def test_service_rejects_sha_after_the_first_hmac(tmp_path: Path, legacy_prefix: bool) -> None:
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path, key="secret")
    if legacy_prefix:
        head = AuditLog(path).append(_event())
        audit.append(_migration({"legacy_count": 1, "legacy_head": head}))
    else:
        audit.append(_event())
    AuditLog(path).append(_event("downgrade"))
    assert audit.verify_chain()
    assert not verify_for_service(audit)


@pytest.mark.parametrize("corruption", ["payload", "algorithm", "not-json"])
def test_service_rejects_a_corrupted_chain(tmp_path: Path, corruption: str) -> None:
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path, key="secret")
    audit.append(_event())
    content = path.read_text(encoding="utf-8")
    if corruption == "payload":
        content = content.replace('"approved":true', '"approved":false')
    elif corruption == "algorithm":
        content = content.replace("hmac-sha256", "unknown")
    else:
        content = "not-json\n"
    path.write_text(content, encoding="utf-8")
    assert not verify_for_service(audit)
