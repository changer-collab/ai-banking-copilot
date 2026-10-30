"""对话历史：按用户隔离、按时间正序回放、列表标题取首条用户消息。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.web.conversation_store import (
    TITLE_LENGTH,
    ConversationStore,
    init_conversation_schema,
)

_T0 = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store(tmp_path / "conversations.db")
    init_conversation_schema(opened)
    yield opened
    opened.close()


def test_messages_are_replayed_in_chronological_order(store: Store) -> None:
    conversations = ConversationStore(store)
    conversations.append(
        user_id="u_demo",
        session_id="s1",
        role="user",
        kind="text",
        text="给王伟转 100 元",
        now=_T0,
    )
    conversations.append(
        user_id="u_demo",
        session_id="s1",
        role="assistant",
        kind="plan_card",
        text="请确认",
        payload={"card": {"card_id": "c1"}},
        now=_T0 + timedelta(seconds=1),
    )

    messages = conversations.list_messages("u_demo", "s1")
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[1].payload == {"card": {"card_id": "c1"}}
    assert messages[0].to_dict()["session_id"] == "s1"


def test_messages_are_isolated_by_user(store: Store) -> None:
    conversations = ConversationStore(store)
    conversations.append(
        user_id="u_demo", session_id="s1", role="user", kind="text", text="主用户", now=_T0
    )
    conversations.append(
        user_id="u_lin", session_id="s1", role="user", kind="text", text="第二用户", now=_T0
    )

    assert [message.text for message in conversations.list_messages("u_lin", "s1")] == ["第二用户"]
    assert [item.title for item in conversations.list_conversations("u_lin")] == ["第二用户"]


def test_list_conversations_groups_and_orders_by_last_active(store: Store) -> None:
    conversations = ConversationStore(store)
    long_text = "  帮我  看看最近三个月的账单明细和订阅扣费情况并给出建议  "
    conversations.append(
        user_id="u_demo",
        session_id="s_old",
        role="user",
        kind="text",
        text=long_text,
        now=_T0,
    )
    conversations.append(
        user_id="u_demo",
        session_id="s_new",
        role="user",
        kind="text",
        text="查余额",
        now=_T0 + timedelta(minutes=5),
    )
    conversations.append(
        user_id="u_demo",
        session_id="s_new",
        role="assistant",
        kind="receipt",
        text="已完成",
        now=_T0 + timedelta(minutes=6),
    )

    summaries = conversations.list_conversations("u_demo")
    assert [item.session_id for item in summaries] == ["s_new", "s_old"]
    assert summaries[0].message_count == 2
    assert summaries[1].title == " ".join(long_text.split())[:TITLE_LENGTH]
    assert summaries[0].last_active_at == (_T0 + timedelta(minutes=6)).isoformat()


def test_list_conversations_falls_back_when_no_user_message(store: Store) -> None:
    conversations = ConversationStore(store)
    conversations.append(
        user_id="u_demo", session_id="s1", role="assistant", kind="error", text="出错了", now=_T0
    )

    summaries = conversations.list_conversations("u_demo")
    assert summaries[0].title == "未命名会话"


def test_delete_removes_only_the_target_user_session(store: Store) -> None:
    conversations = ConversationStore(store)
    conversations.append(
        user_id="u_demo", session_id="s1", role="user", kind="text", text="a", now=_T0
    )
    conversations.append(
        user_id="u_demo", session_id="s1", role="assistant", kind="text", text="b", now=_T0
    )
    conversations.append(
        user_id="u_lin", session_id="s1", role="user", kind="text", text="c", now=_T0
    )

    assert conversations.delete("u_demo", "s1") == 2
    assert conversations.list_messages("u_demo", "s1") == ()
    assert len(conversations.list_messages("u_lin", "s1")) == 1
    assert conversations.delete("u_demo", "s1") == 0
