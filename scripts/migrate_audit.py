"""停服务后接收旧 SHA 历史；条数与链头必须来自运维独立核对的可信锚。

密钥只读 AIB_AUDIT_KEY。先独占创建 <path>.bak，再追加 HMAC 迁移事件。
迁移不能追溯证明旧 SHA 历史未被改写；不处理日志截断或密钥轮换。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from ai_banking_copilot.policy.audit import ALG_SHA256, AuditEvent, AuditLog, ChainBrokenError
from ai_banking_copilot.policy.audit_validation import KIND_AUDIT_MIGRATION


def migrate(path: Path, *, expected_count: int, expected_head: str, key: str) -> Path:
    """验证外部锚与纯 SHA 链后迁移；校验或备份失败时不写原文件。"""
    if not key.strip():
        raise ValueError("请通过 AIB_AUDIT_KEY 提供审计密钥")
    if not path.is_file():
        raise ValueError("审计日志不存在")
    original = path.read_bytes()
    audit = AuditLog(path, key=key)
    if not audit.verify_chain():
        raise ValueError("审计链损坏或密钥不匹配")
    entries = [json.loads(line) for line in original.decode("utf-8").splitlines() if line.strip()]
    if not entries or any(entry.get("alg", ALG_SHA256) != ALG_SHA256 for entry in entries):
        raise ValueError("只能迁移非空的纯 SHA 历史链；混存或 HMAC 链不执行迁移")
    if len(entries) != expected_count or audit.head() != expected_head:
        raise ValueError("外部可信锚的条数或链头与日志不匹配")

    backup = Path(f"{path}.bak")
    # xb 不覆盖已有备份；备份完整落盘后才允许改动原日志。
    with backup.open("xb") as handle:
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())
    if not original.endswith(b"\n"):
        with path.open("ab") as handle:
            handle.write(b"\n")
    audit.append(
        AuditEvent(
            event_id=f"audit-migration-{uuid4().hex}",
            kind=KIND_AUDIT_MIGRATION,
            payload={"legacy_count": expected_count, "legacy_head": expected_head},
            prev_hash=expected_head,
        )
    )
    return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="停服务后显式迁移旧 SHA 审计链（需要独立可信锚）")
    parser.add_argument("--path", type=Path, required=True, help="旧审计 JSONL 文件")
    parser.add_argument("--expected-count", type=int, required=True, help="独立核对的旧条数")
    parser.add_argument("--expected-head", required=True, help="独立核对的旧链头")
    args = parser.parse_args(argv)
    try:
        backup = migrate(
            args.path,
            expected_count=args.expected_count,
            expected_head=args.expected_head,
            key=os.environ.get("AIB_AUDIT_KEY", ""),
        )
    except (OSError, ValueError, ChainBrokenError):
        # 不回显异常正文或环境变量，避免把密钥带入终端和日志。
        print(
            "迁移失败：请检查日志、可信锚、AIB_AUDIT_KEY 与备份路径；未自动接收历史。",
            file=sys.stderr,
        )
        return 1
    print(f"迁移完成；历史备份：{backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
