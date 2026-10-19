from pathlib import Path

import pytest

from ai_banking_copilot.compose import build_audit_log
from ai_banking_copilot.policy.audit import AuditEvent, AuditLog, ChainBrokenError


def test_service_rejects_a_valid_recomputed_sha_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    # 攻击者不需要密钥，就能重写整条 SHA 链并使旧的数学检查通过。
    forged = AuditLog(path)
    forged.append(AuditEvent("forged", "execution", {"approved": True}, ""))
    assert forged.verify_chain()

    with pytest.raises(ChainBrokenError):
        build_audit_log(tmp_path / "bank.db", {"AIB_AUDIT_KEY": "service-secret"})


def test_service_rejects_an_empty_key_file(tmp_path: Path) -> None:
    (tmp_path / "audit.jsonl.key").write_text("  ", encoding="utf-8")
    with pytest.raises(ChainBrokenError):
        build_audit_log(tmp_path / "bank.db", {})
