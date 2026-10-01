"""追加式审计链：JSONL + sha256 哈希链（**不占用 01 的 SQLite schema**）。

范围声明（H5，必须如实）：审计链只证「**过程合规 + 可追溯**」，**不证「决策正确」**；
签名来自 TCB（本地哈希链），**不来自 LLM**。

记录字段对齐 PRD §5.6 / §9.1：taint 标签 · `intent_ref` · 命中规则 id · 数值出处
`record_id` · 授权级别 · 执行结果 · 决策链路。链路结构：每行 JSON 记录
`{event_id, kind, payload, prev_hash, entry_hash}`，`entry_hash = sha256(canonical(前四者))`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ai_banking_copilot.gateway.hashing import canonical_json, sha256_hex

GENESIS_HASH: str = "0" * 64
"""创世前驱哈希：`prev_hash` 为空的语义等价物。"""

KIND_DECISION = "decision"
"""裁决事件（含级别、原因码、决策链路）。"""

KIND_EXECUTION = "execution"
"""执行事件（含回执、出处 record_id、执行结果）。"""

KIND_BREAKER = "breaker"
"""熔断事件（含触发项）。"""

KIND_REVERSAL = "reversal"
"""撤销 / 补偿事件（竞态须如实记录"已扣 + 已发起退回"）。"""

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

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

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
        entry_hash = _entry_hash(event, head)
        record = _record(event, head)
        record["entry_hash"] = entry_hash
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(record) + "\n")
        return entry_hash

    def verify_chain(self) -> bool:
        """重算全链哈希并校验链接关系；任一不符返回 `False`（不抛异常）。"""
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
            if _entry_hash(event, previous) != str(entry["entry_hash"]):
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


def _entry_hash(event: AuditEvent, prev_hash: str) -> str:
    return sha256_hex(canonical_json(_record(event, prev_hash)))


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
