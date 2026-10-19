import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_banking_copilot.compose import build_audit_log
from ai_banking_copilot.policy.audit import GENESIS_HASH, AuditEvent, AuditLog

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrate_audit.py"
KEY = "migration-test-secret"


def _legacy(path: Path) -> tuple[int, str]:
    audit = AuditLog(path)
    audit.append(AuditEvent("old-1", "execution", {}, ""))
    head = audit.append(AuditEvent("old-2", "execution", {}, ""))
    return 2, head


def _run(path: Path, count: int, head: str, *, key: str = KEY) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["AIB_AUDIT_KEY"] = key
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--path", str(path),
            "--expected-count", str(count),
            "--expected-head", head,
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@pytest.mark.parametrize("trailing_newline", [False, True])
def test_migration_backs_up_and_binds_an_external_anchor(
    tmp_path: Path, trailing_newline: bool
) -> None:
    path = tmp_path / "audit.jsonl"
    count, head = _legacy(path)
    if not trailing_newline:
        path.write_bytes(path.read_bytes().rstrip(b"\r\n"))
    original = path.read_bytes()
    result = _run(path, count, head)
    assert result.returncode == 0, result.stderr
    assert KEY not in result.stdout + result.stderr
    assert Path(f"{path}.bak").read_bytes() == original
    assert path.read_bytes().startswith(original)
    audit = build_audit_log(tmp_path / "bank.db", {"AIB_AUDIT_KEY": KEY})
    event = audit.events()[-1]
    assert event.kind == "audit_migration"
    assert event.payload == {"legacy_count": count, "legacy_head": head}
    assert len(audit.events()) == count + 1


@pytest.mark.parametrize("bad_count,bad_head", [(1, None), (2, GENESIS_HASH)])
def test_migration_rejects_an_incorrect_external_anchor_without_changes(
    tmp_path: Path, bad_count: int, bad_head: str | None
) -> None:
    path = tmp_path / "audit.jsonl"
    _, head = _legacy(path)
    original = path.read_bytes()
    result = _run(path, bad_count, bad_head or head)
    assert result.returncode != 0
    assert path.read_bytes() == original
    assert not Path(f"{path}.bak").exists()


@pytest.mark.parametrize("failure", ["mixed", "hmac", "corrupt", "backup", "key", "empty"])
def test_migration_failure_never_appends_or_overwrites_backup(tmp_path: Path, failure: str) -> None:
    path = tmp_path / "audit.jsonl"
    count, head = _legacy(path)
    if failure == "mixed":
        head = AuditLog(path, key=KEY).append(AuditEvent("new", "execution", {}, ""))
        count += 1
    elif failure == "hmac":
        path.unlink()
        head = AuditLog(path, key=KEY).append(AuditEvent("new", "execution", {}, ""))
        count = 1
    elif failure == "corrupt":
        path.write_text("not-json\n", encoding="utf-8")
    elif failure == "empty":
        path.write_bytes(b"")
        count, head = 0, GENESIS_HASH
    backup = Path(f"{path}.bak")
    if failure == "backup":
        backup.write_bytes(b"do-not-overwrite")
    original = path.read_bytes()
    result = _run(path, count, head, key="" if failure == "key" else KEY)
    assert result.returncode != 0
    assert KEY not in result.stdout + result.stderr
    assert path.read_bytes() == original
    if failure == "backup":
        assert backup.read_bytes() == b"do-not-overwrite"
    else:
        assert not backup.exists()


def test_migration_requires_explicit_external_anchor(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    _legacy(path)
    original = path.read_bytes()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--path", str(path)],
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert path.read_bytes() == original
