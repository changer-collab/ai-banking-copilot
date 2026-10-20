"""把真实 HTTP 元数据按执行身份收集；不保存请求或响应正文。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

from ai_banking_copilot.planning.call_observation import CallObservation

_TASK_ID: ContextVar[str | None] = ContextVar("model_task_id", default=None)


class TaskObservations:
    def __init__(self) -> None:
        self._lock = Lock()
        self._records: dict[str, list[dict[str, object]]] = {}

    @contextmanager
    def scope(self, execution_id: str) -> Iterator[None]:
        token = _TASK_ID.set(execution_id)
        try:
            yield
        finally:
            _TASK_ID.reset(token)

    def __call__(self, observation: CallObservation) -> None:
        identity = observation.task_id or _TASK_ID.get()
        if identity is None:
            return
        record = observation.public_dict()
        record["task_id"] = identity
        with self._lock:
            self._records.setdefault(identity, []).append(record)

    def drain(self, execution_id: str) -> list[dict[str, object]]:
        with self._lock:
            return self._records.pop(execution_id, [])
