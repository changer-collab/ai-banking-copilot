"""服务入口的算法策略；摘要与签名校验仍由原 AuditLog 完成。"""

from __future__ import annotations

import json

from ai_banking_copilot.policy.audit import (
    ALG_HMAC_SHA256,
    ALG_SHA256,
    GENESIS_HASH,
    AuditLog,
    ChainBrokenError,
)

KIND_AUDIT_MIGRATION = "audit_migration"


def verify_for_service(audit: AuditLog) -> bool:
    """只接受完整 HMAC 链，或由 HMAC 迁移事件封住的连续 SHA 历史前缀。"""
    try:
        if not audit.keyed or not audit.verify_chain():
            return False
        if not audit.path.is_file():
            return True
        legacy_count = 0
        legacy_head = GENESIS_HASH
        hmac_started = False
        for line in audit.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            alg = entry.get("alg", ALG_SHA256)
            if alg == ALG_SHA256:
                if hmac_started:
                    return False
                legacy_count += 1
                legacy_head = entry["entry_hash"]
                continue
            if alg != ALG_HMAC_SHA256:
                return False
            if not hmac_started and legacy_count:
                payload = entry["payload"]
                if (
                    entry["kind"] != KIND_AUDIT_MIGRATION
                    or type(payload.get("legacy_count")) is not int
                    or payload != {"legacy_count": legacy_count, "legacy_head": legacy_head}
                ):
                    return False
            hmac_started = True
        return hmac_started or legacy_count == 0
    except (ChainBrokenError, OSError, ValueError):
        return False
