"""会话持久化外壳；仅保存数据，不携带连接、锁或可执行对象。"""

from __future__ import annotations

from dataclasses import dataclass

SESSION_SNAPSHOT_VERSION = 2
"""快照结构版本：2 起计划卡带服务端 `expires_at`（G-02），旧快照 fail-closed 拒绝。"""


class SessionSnapshotError(ValueError):
    """快照版本、身份或内容校验失败。"""


@dataclass(frozen=True)
class SessionSnapshot:
    version: int
    user_id: str
    session_id: str
    revision: int
    execution_id: str
    state: str
    payload: dict[str, object]

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != SESSION_SNAPSHOT_VERSION:
            raise SessionSnapshotError("unsupported_snapshot_version")
        if any(not isinstance(value, str) or not value.strip() for value in (
            self.user_id, self.session_id, self.execution_id,
        )):
            raise SessionSnapshotError("invalid_snapshot_identity")
        if type(self.revision) is not int or self.revision < 0:
            raise SessionSnapshotError("invalid_snapshot_revision")
        if not isinstance(self.state, str) or self.state not in {
            "idle", "needs_clarification", "blocked", "awaiting_confirm", "executing",
            "interrupted", "completed", "partial", "failed", "taken_over", "reversed",
        }:
            raise SessionSnapshotError("invalid_snapshot_state")
        if not isinstance(self.payload, dict):
            raise SessionSnapshotError("invalid_snapshot_payload")
