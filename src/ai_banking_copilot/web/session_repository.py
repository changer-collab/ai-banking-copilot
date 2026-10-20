"""独立快照表和 revision CAS；不改变银行事实 schema。"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Protocol

from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.web.session_codec import decode_snapshot, encode_snapshot
from ai_banking_copilot.web.session_snapshot import SessionSnapshot

SESSION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_snapshots (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    snapshot_json TEXT NOT NULL,
    PRIMARY KEY (user_id, session_id)
);
"""


class SessionPersistenceError(RuntimeError):
    """稳定的持久化失败（含 CAS 冲突）；原快照保持不变。"""


class SessionRepository(Protocol):
    def load(self, user_id: str, session_id: str) -> SessionSnapshot | None: ...

    def save(
        self, snapshot: SessionSnapshot, *, expected_revision: int | None,
    ) -> SessionSnapshot: ...


def init_session_schema(store: Store) -> None:
    with store.transaction() as conn:
        conn.execute(SESSION_SCHEMA_SQL)


class SQLiteSessionRepository:
    def __init__(self, store: Store) -> None:
        self._store = store

    def load(self, user_id: str, session_id: str) -> SessionSnapshot | None:
        try:
            rows = self._store.query(
                "SELECT revision, snapshot_json FROM session_snapshots "
                "WHERE user_id = ? AND session_id = ?", (user_id, session_id),
            )
            if not rows:
                return None
            snapshot = decode_snapshot(str(rows[0]["snapshot_json"]),
                                       user_id=user_id, session_id=session_id)
            if snapshot.revision != rows[0]["revision"]:
                raise SessionPersistenceError("session_revision_mismatch")
            return snapshot
        except sqlite3.Error as error:
            raise SessionPersistenceError("session_load_failed") from error

    def save(
        self, snapshot: SessionSnapshot, *, expected_revision: int | None,
    ) -> SessionSnapshot:
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 1
        ):
            raise SessionPersistenceError("session_revision_conflict")
        if snapshot.revision != (expected_revision or 0):
            raise SessionPersistenceError("session_revision_conflict")
        saved = replace(snapshot, revision=(expected_revision or 0) + 1)
        encoded = encode_snapshot(saved)
        try:
            with self._store.transaction() as conn:
                if expected_revision is None:
                    conn.execute(
                        "INSERT INTO session_snapshots VALUES (?, ?, ?, ?)",
                        (saved.user_id, saved.session_id, saved.revision, encoded),
                    )
                else:
                    cursor = conn.execute(
                        "UPDATE session_snapshots SET revision = ?, snapshot_json = ? "
                        "WHERE user_id = ? AND session_id = ? AND revision = ?",
                        (
                            saved.revision, encoded, saved.user_id,
                            saved.session_id, expected_revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise SessionPersistenceError("session_revision_conflict")
        except sqlite3.IntegrityError as error:
            raise SessionPersistenceError("session_revision_conflict") from error
        except sqlite3.Error as error:
            raise SessionPersistenceError("session_save_failed") from error
        return saved
