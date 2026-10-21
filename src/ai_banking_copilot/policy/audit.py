"""追加式审计链：JSONL + 哈希链（**不占用 01 的 SQLite schema**）。

范围声明（H5，必须如实）：审计链只证「**过程合规 + 可追溯**」，**不证「决策正确」**；
签名来自 TCB（本地哈希链），**不来自 LLM**。

两种签名模式（逐行记录 `alg`，可混存、可迁移）：
- `sha256`（默认，无密钥）：与历史日志兼容；仅能发现「改了行却忘了重算」这类**偶然**损坏；
- `hmac-sha256`（提供密钥时）：`hmac(key, canonical(前四者))`，掌握日志文件写权限但**拿不到
  密钥**者无法整体重算。

**如实声明其局限**：密钥默认存放在审计日志同目录（`audit.jsonl.key`），具备该目录读写权限者
可同时改写日志与密钥——因此这一层**不是**抗抵赖（需要外部 KMS / HSM 或受控 TCB）。生产环境应
用 `AIB_AUDIT_KEY` 从密钥管理服务注入，并把审计日志投递到独立存储。

记录字段对齐 PRD §5.6 / §9.1：taint 标签 · `intent_ref` · 命中规则 id · 数值出处
`record_id` · 授权级别 · 执行结果 · 决策链路。链路结构：每行 JSON 记录
`{event_id, kind, payload, prev_hash, entry_hash}`（HMAC 模式额外带 `alg`，`alg` **不参与**
哈希计算，改动它会导致校验失败）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path

from ai_banking_copilot.gateway.hashing import canonical_json, sha256_hex

GENESIS_HASH: str = "0" * 64
"""创世前驱哈希：`prev_hash` 为空的语义等价物。"""

ALG_SHA256 = "sha256"
"""无密钥模式（默认，兼容历史日志）。"""

ALG_HMAC_SHA256 = "hmac-sha256"
"""带密钥模式：`hmac(key, canonical(record))`。"""

_ALG_FIELD = "alg"
"""签名算法标记字段名（**不参与**哈希计算，改动即校验失败）。"""

KIND_DECISION = "decision"
"""裁决事件（含级别、原因码、决策链路）。"""

KIND_EXECUTION = "execution"
"""执行事件（含回执、出处 record_id、执行结果）。"""

KIND_BREAKER = "breaker"
"""熔断事件（含触发项）。"""

KIND_REVERSAL = "reversal"
"""撤销 / 补偿事件（未真正落地时须记 `compensation_unavailable`，不得记为「已发起退回」）。"""

KIND_OPERATOR = "operator"
"""**运维动作**事件（裁决 #27）：含动作名、理由、动作前后的状态——解锁熔断等降级出口必须留痕。"""

KIND_TOOL_HOOK = "tool_hook"
"""**Pre-Tool Hook** 事件：写操作被拦截时的工具名、操作类型、计划 ID、拦截原因与确认状态。"""

_REQUIRED_KEYS = ("event_id", "kind", "payload", "prev_hash", "entry_hash")


class ChainBrokenError(RuntimeError):
    """哈希链断裂：`prev_hash` 与链头不一致，或审计行被篡改 / 非 JSON 对象。"""


@dataclass(frozen=True)
class AuditEvent:
    """一条待追加的审计事件。`prev_hash` 为空串时由 `AuditLog` 自动链接链头。"""

    event_id: str
    kind: str
    payload: dict[str, object]
    prev_hash: str

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("event_id 不得为空")
        if not self.kind:
            raise ValueError("kind 不得为空")
        if self.prev_hash and len(self.prev_hash) != 64:
            raise ValueError("prev_hash 必须是 64 位十六进制或空串")


class AuditLog:
    """追加式审计链：**只追加、不修改**历史行。"""

    def __init__(self, path: str | Path, *, key: str | None = None) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._key = key.strip() if key and key.strip() else None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def keyed(self) -> bool:
        """是否处于带密钥（`hmac-sha256`）模式。"""
        return self._key is not None

    def head(self) -> str:
        """当前链头哈希；空链返回 `GENESIS_HASH`。"""
        entries = self._read_entries()
        return str(entries[-1]["entry_hash"]) if entries else GENESIS_HASH

    def append(self, event: AuditEvent) -> str:
        """追加一条事件，返回本条 `entry_hash`（即新链头）。"""
        head = self.head()
        if event.prev_hash and event.prev_hash != head:
            raise ChainBrokenError(
                f"审计链断裂：事件 {event.event_id} 声明 prev_hash={event.prev_hash}，链头={head}"
            )
        alg = ALG_HMAC_SHA256 if self._key is not None else ALG_SHA256
        entry_hash = _entry_hash(event, head, alg=alg, key=self._key)
        record = _record(event, head)
        if alg != ALG_SHA256:
            record[_ALG_FIELD] = alg
        record["entry_hash"] = entry_hash
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(record) + "\n")
        return entry_hash

    def verify_chain(self) -> bool:
        """重算全链并校验链接关系；任一不符返回 `False`（不抛异常）。

        逐行按该行声明的 `alg` 重算：`hmac-sha256` 行在**无密钥**时无法校验 → 返回 `False`
        （fail-closed，不静默放过）。
        """
        try:
            entries = self._read_entries()
        except ChainBrokenError:
            return False
        previous = GENESIS_HASH
        for entry in entries:
            if str(entry["prev_hash"]) != previous:
                return False
            try:
                event = _event_of(entry, previous)
            except ChainBrokenError:
                return False
            alg = str(entry.get(_ALG_FIELD, ALG_SHA256))
            if alg not in (ALG_SHA256, ALG_HMAC_SHA256):
                return False
            if alg == ALG_HMAC_SHA256 and self._key is None:
                return False
            if _entry_hash(event, previous, alg=alg, key=self._key) != str(entry["entry_hash"]):
                return False
            previous = str(entry["entry_hash"])
        return True

    def events(self) -> tuple[AuditEvent, ...]:
        """按写入顺序返回全部事件（行损坏时抛 `ChainBrokenError`，fail-closed）。"""
        return tuple(_event_of(entry, str(entry["prev_hash"])) for entry in self._read_entries())

    def _read_entries(self) -> list[dict[str, object]]:
        if not self._path.is_file():
            return []
        return [
            _parse_line(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def _record(event: AuditEvent, prev_hash: str) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "kind": event.kind,
        "payload": event.payload,
        "prev_hash": prev_hash,
    }


def _entry_hash(
    event: AuditEvent, prev_hash: str, *, alg: str = ALG_SHA256, key: str | None = None
) -> str:
    """按 `alg` 计算本条摘要：无密钥用 sha256，有密钥用 `hmac-sha256`。"""
    payload = canonical_json(_record(event, prev_hash))
    if alg == ALG_HMAC_SHA256:
        if key is None:
            raise ChainBrokenError("`hmac-sha256` 模式缺少密钥：无法计算摘要")
        return hmac.new(key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return sha256_hex(payload)


def _event_of(entry: dict[str, object], prev_hash: str) -> AuditEvent:
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        raise ChainBrokenError("审计行 payload 不是 JSON 对象")
    return AuditEvent(
        event_id=str(entry["event_id"]),
        kind=str(entry["kind"]),
        payload={str(key): value for key, value in payload.items()},
        prev_hash=prev_hash,
    )


def _parse_line(line: str) -> dict[str, object]:
    try:
        data = json.loads(line)
    except ValueError as error:
        raise ChainBrokenError(f"审计行不是合法 JSON：{line[:40]!r}") from error
    if not isinstance(data, dict):
        raise ChainBrokenError("审计行不是 JSON 对象")
    parsed = {str(key): value for key, value in data.items()}
    for key in _REQUIRED_KEYS:
        if key not in parsed:
            raise ChainBrokenError(f"审计行缺少字段：{key}")
    return parsed
