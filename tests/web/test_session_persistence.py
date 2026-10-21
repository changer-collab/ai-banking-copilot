from __future__ import annotations

import threading

import pytest

from ai_banking_copilot.gateway.store import Store, init_schema
from ai_banking_copilot.web.session_manager import SessionManager
from ai_banking_copilot.web.session_repository import (
    SessionPersistenceError,
    SQLiteSessionRepository,
    init_session_schema,
)
from ai_banking_copilot.web.session_snapshot import SESSION_SNAPSHOT_VERSION, SessionSnapshot


def _snap(revision: int = 0) -> SessionSnapshot:
    return SessionSnapshot(SESSION_SNAPSHOT_VERSION, "u1", "s1", revision, "e1", "idle", {"x": 1})


def test_repository_cas_and_restart(tmp_path) -> None:
    store = Store(tmp_path / "db.sqlite")
    init_schema(store)
    init_session_schema(store)
    repo = SQLiteSessionRepository(store)
    saved = repo.save(_snap(), expected_revision=None)
    assert saved.revision == 1
    assert repo.load("u1", "s1").revision == 1
    with pytest.raises(SessionPersistenceError):
        repo.save(_snap(), expected_revision=None)
    with pytest.raises(SessionPersistenceError):
        repo.save(_snap(1), expected_revision=0)
    saved2 = repo.save(_snap(1), expected_revision=1)
    assert saved2.revision == 2
    store.close()


def test_manager_creates_one_instance_under_race() -> None:
    calls = 0
    lock = threading.Lock()

    def factory(user_id, session_id, shared_lock):
        nonlocal calls
        with lock:
            calls += 1
        return object()

    manager = SessionManager(None, factory=factory)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(manager.get("u", "s")))
        for _ in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == 1
    assert len({id(item) for item in results}) == 1
