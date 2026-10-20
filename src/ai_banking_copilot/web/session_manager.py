"""单进程会话缓存和全局可重入锁；不提供多 worker 执行互斥。"""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock
from typing import Generic, TypeVar

from ai_banking_copilot.web.session_repository import SessionRepository
from ai_banking_copilot.web.session_snapshot import SessionSnapshot

T = TypeVar("T")
PROCESS_SESSION_LOCK = RLock()


class SessionManager(Generic[T]):
    def __init__(
        self,
        repository: SessionRepository | None,
        *,
        factory: Callable[[str, str, RLock], T],
        restore: Callable[[SessionSnapshot, RLock], T] | None = None,
        lock: RLock | None = None,
    ) -> None:
        self._repository = repository
        self._factory = factory
        self._restore = restore
        self.lock = lock if lock is not None else PROCESS_SESSION_LOCK
        self._sessions: dict[tuple[str, str], T] = {}

    def get(self, user_id: str, session_id: str) -> T:
        if not user_id or not session_id:
            raise ValueError("invalid_session_identity")
        with self.lock:
            key = (user_id, session_id)
            if key not in self._sessions:
                snapshot = (
                    self._repository.load(user_id, session_id)
                    if self._repository is not None else None
                )
                if snapshot is not None:
                    if self._restore is None:
                        raise ValueError("session_restore_callback_required")
                    session = self._restore(snapshot, self.lock)
                else:
                    session = self._factory(user_id, session_id, self.lock)
                self._sessions[key] = session
            return self._sessions[key]
