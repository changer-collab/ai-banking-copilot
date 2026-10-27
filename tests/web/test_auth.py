"""登录凭证层：口令不落明文、会话只存哈希、失败可收敛、票据一次性。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.web.auth import (
    DEMO_ACCOUNTS,
    LOCK_BASE_SECONDS,
    LOCK_MAX_SECONDS,
    TICKET_PURPOSE_DELETE_CONVERSATION,
    TICKET_PURPOSE_UNBIND,
    AuthInvalidCredentialsError,
    AuthLockedError,
    AuthService,
    AuthTicketInvalidError,
    init_auth_schema,
    lock_seconds,
)

_PASSWORD = "demo-pass-1027"
_T0 = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store(tmp_path / "auth.db")
    init_auth_schema(opened)
    yield opened
    opened.close()


def _service(store: Store) -> AuthService:
    service = AuthService(store, clock=lambda: _T0)
    service.ensure_demo_accounts(_PASSWORD)
    return service


def test_ensure_demo_accounts_is_idempotent(store: Store) -> None:
    service = _service(store)
    assert service.ensure_demo_accounts("another-pass") == ()

    for username, user_id, display_name, phone_tail in DEMO_ACCOUNTS:
        user = service.user_by_username(username)
        assert user is not None
        assert (user.user_id, user.display_name, user.phone_tail) == (
            user_id,
            display_name,
            phone_tail,
        )
    # 口令未被二次播种覆盖：首轮口令仍可登录。
    assert service.login("chenming", _PASSWORD, now=_T0).user.user_id == "u_demo"


def test_password_is_not_stored_in_plaintext(store: Store) -> None:
    _service(store)
    rows = store.query("SELECT password_hash, salt FROM auth_users WHERE username = 'chenming'")
    assert _PASSWORD not in str(rows[0]["password_hash"])
    assert rows[0]["salt"]


def test_login_resolve_and_logout(store: Store) -> None:
    service = _service(store)
    result = service.login(
        "linjing", _PASSWORD, now=_T0, user_agent="pytest", client_ip="127.0.0.1"
    )

    assert result.user.user_id == "u_lin"
    assert result.session.expires_at > result.session.issued_at

    session = service.resolve(result.session.token, now=_T0)
    assert session is not None
    assert session.user_id == "u_lin"

    assert service.logout(result.session.token, now=_T0) is True
    assert service.logout(result.session.token, now=_T0) is False
    assert service.resolve(result.session.token, now=_T0) is None


def test_resolve_rejects_expired_token(store: Store) -> None:
    service = _service(store)
    result = service.login("chenming", _PASSWORD, now=_T0)
    assert service.resolve(result.session.token, now=result.session.expires_at) is None


def test_resolve_slides_expiry_when_close_to_timeout(store: Store) -> None:
    service = _service(store)
    result = service.login("chenming", _PASSWORD, now=_T0)
    late = result.session.expires_at - timedelta(seconds=10)

    renewed = service.resolve(result.session.token, now=late)
    assert renewed is not None
    assert renewed.expires_at > result.session.expires_at


def test_wrong_password_uses_same_error_as_unknown_account(store: Store) -> None:
    service = _service(store)
    with pytest.raises(AuthInvalidCredentialsError):
        service.login("chenming", "wrong", now=_T0)
    with pytest.raises(AuthInvalidCredentialsError):
        service.login("nobody", _PASSWORD, now=_T0)


def test_repeated_failures_lock_the_account(store: Store) -> None:
    service = _service(store)
    for _ in range(5):
        with pytest.raises(AuthInvalidCredentialsError):
            service.login("chenming", "wrong", now=_T0)

    with pytest.raises(AuthLockedError):
        service.login("chenming", _PASSWORD, now=_T0)

    # 锁定到期后可以正常登录，且失败计数被清零。
    unlocked = _T0 + timedelta(seconds=LOCK_BASE_SECONDS + 1)
    assert service.login("chenming", _PASSWORD, now=unlocked).user.user_id == "u_demo"


def test_lock_seconds_grows_then_caps() -> None:
    assert lock_seconds(1) == LOCK_BASE_SECONDS
    assert lock_seconds(2) == LOCK_BASE_SECONDS * 2
    assert lock_seconds(9) == LOCK_MAX_SECONDS


def test_verify_password_does_not_count_toward_lock(store: Store) -> None:
    service = _service(store)
    for _ in range(10):
        assert service.verify_password("u_demo", "wrong") is False
    assert service.verify_password("u_demo", _PASSWORD) is True
    assert service.verify_password("u_unknown", _PASSWORD) is False
    assert service.login("chenming", _PASSWORD, now=_T0).user.user_id == "u_demo"


def test_ticket_is_single_use_and_purpose_bound(store: Store) -> None:
    service = _service(store)
    ticket = service.issue_ticket("u_demo", TICKET_PURPOSE_DELETE_CONVERSATION, now=_T0)

    with pytest.raises(AuthTicketInvalidError):
        service.consume_ticket("u_demo", TICKET_PURPOSE_UNBIND, ticket, now=_T0)
    with pytest.raises(AuthTicketInvalidError):
        service.consume_ticket("u_lin", TICKET_PURPOSE_DELETE_CONVERSATION, ticket, now=_T0)

    service.consume_ticket("u_demo", TICKET_PURPOSE_DELETE_CONVERSATION, ticket, now=_T0)
    with pytest.raises(AuthTicketInvalidError):
        service.consume_ticket("u_demo", TICKET_PURPOSE_DELETE_CONVERSATION, ticket, now=_T0)


def test_ticket_expires_and_empty_ticket_is_rejected(store: Store) -> None:
    service = _service(store)
    ticket = service.issue_ticket("u_demo", TICKET_PURPOSE_DELETE_CONVERSATION, now=_T0)

    with pytest.raises(AuthTicketInvalidError):
        service.consume_ticket(
            "u_demo",
            TICKET_PURPOSE_DELETE_CONVERSATION,
            ticket,
            now=_T0 + timedelta(seconds=301),
        )
    with pytest.raises(AuthTicketInvalidError):
        service.consume_ticket("u_demo", TICKET_PURPOSE_DELETE_CONVERSATION, "", now=_T0)


def test_recent_logins_returns_newest_first(store: Store) -> None:
    service = _service(store)
    service.login("chenming", _PASSWORD, now=_T0, user_agent="first")
    service.login("chenming", _PASSWORD, now=_T0 + timedelta(minutes=1), user_agent="second")

    records = service.recent_logins("u_demo", limit=1)
    assert len(records) == 1
    assert records[0].user_agent == "second"
    assert records[0].revoked is False
