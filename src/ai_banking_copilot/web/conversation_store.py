"""对话历史：**独立消息表**，不扩会话快照。

快照的 `payload` 是状态机现场（白名单字段 + 强校验），消息是多条流水，
塞进去只会让 CAS 变重、回滚变难，因此单独建表：

- 写入点只在接口层（`app.py`）；写失败不影响主流程；
- 读取按 `user_id + session_id` 过滤，跨用户读不到；
- 列表按 `last_active_at` 倒序，标题取首条用户消息前 20 字。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from ai_banking_copilot.gateway.store import Store

CONVERSATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    kind         TEXT NOT NULL,
    text         TEXT NOT NULL,
    payload_json TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_user
    ON conversation_messages(user_id, session_id, created_at);
"""

TITLE_LENGTH = 20
"""列表标题长度：首条用户消息截前 20 字。"""

LIST_LIMIT = 50
"""会话列表上限：演示规模足够，避免一次拉全库。"""


@dataclass(frozen=True)
class ConversationMessage:
    """一条对话消息。`payload` 承载结构化卡片（计划卡等）。"""

    message_id: str
    user_id: str
    session_id: str
    role: str
    kind: str
    text: str
    payload: dict[str, object] | None
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "session_id": self.session_id,
            "role": self.role,
            "kind": self.kind,
            "text": self.text,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ConversationSummary:
    """左栏列表项。"""

    session_id: str
    title: str
    message_count: int
    last_active_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "message_count": self.message_count,
            "last_active_at": self.last_active_at,
        }


class ConversationStore:
    """对话消息读写。**只做存储**，不做状态判定。"""

    def __init__(self, store: Store, *, clock: Callable[[], datetime] | None = None) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def append(
        self,
        *,
        user_id: str,
        session_id: str,
        role: str,
        kind: str,
        text: str,
        payload: dict[str, object] | None = None,
        now: datetime | None = None,
    ) -> ConversationMessage:
        created_at = (now if now is not None else self._clock()).isoformat()
        message = ConversationMessage(
            message_id=f"msg_{secrets.token_hex(8)}",
            user_id=user_id,
            session_id=session_id,
            role=role,
            kind=kind,
            text=text,
            payload=payload,
            created_at=created_at,
        )
        payload_json = (
            None if payload is None else json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
        with self._store.transaction() as conn:
            conn.execute(
                "INSERT INTO conversation_messages"
                " (message_id, user_id, session_id, role, kind, text, payload_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.message_id,
                    message.user_id,
                    message.session_id,
                    message.role,
                    message.kind,
                    message.text,
                    payload_json,
                    message.created_at,
                ),
            )
        return message

    def list_messages(
        self, user_id: str, session_id: str, *, limit: int = 200
    ) -> tuple[ConversationMessage, ...]:
        """取最近 `limit` 条，返回时按时间正序（前端直接追加）。"""

        rows = self._store.query(
            "SELECT message_id, user_id, session_id, role, kind, text, payload_json, created_at"
            " FROM conversation_messages WHERE user_id = ? AND session_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (user_id, session_id, limit),
        )
        return tuple(_message_from_row(row) for row in reversed(rows))

    def list_conversations(
        self, user_id: str, *, limit: int = LIST_LIMIT
    ) -> tuple[ConversationSummary, ...]:
        rows = self._store.query(
            "SELECT session_id, COUNT(*) AS message_count, MAX(created_at) AS last_active_at,"
            "  (SELECT text FROM conversation_messages recent"
            "     WHERE recent.user_id = grouped.user_id"
            "       AND recent.session_id = grouped.session_id"
            "       AND recent.role = 'user'"
            "     ORDER BY recent.created_at ASC, recent.rowid ASC LIMIT 1) AS first_user_text"
            " FROM conversation_messages grouped WHERE user_id = ?"
            " GROUP BY session_id ORDER BY last_active_at DESC LIMIT ?",
            (user_id, limit),
        )
        return tuple(
            ConversationSummary(
                session_id=str(row["session_id"]),
                title=_title(str(row["first_user_text"] or "")),
                message_count=int(row["message_count"]),
                last_active_at=str(row["last_active_at"]),
            )
            for row in rows
        )

    def delete(self, user_id: str, session_id: str) -> int:
        """删除某会话的全部消息；返回删除条数。"""

        with self._store.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM conversation_messages WHERE user_id = ? AND session_id = ?",
                (user_id, session_id),
            )
            return cursor.rowcount


def init_conversation_schema(store: Store) -> None:
    """建对话消息表（幂等）：组合根在写连接上调用一次即可。"""

    with store.transaction() as conn:
        conn.executescript(CONVERSATION_SCHEMA_SQL)


def _message_from_row(row: sqlite3.Row) -> ConversationMessage:
    raw = row["payload_json"]
    payload = None if raw is None else cast(dict[str, object], json.loads(str(raw)))
    return ConversationMessage(
        message_id=str(row["message_id"]),
        user_id=str(row["user_id"]),
        session_id=str(row["session_id"]),
        role=str(row["role"]),
        kind=str(row["kind"]),
        text=str(row["text"]),
        payload=payload,
        created_at=str(row["created_at"]),
    )


def _title(text: str) -> str:
    cleaned = " ".join(text.split())
    if not cleaned:
        return "未命名会话"
    return cleaned[:TITLE_LENGTH]
