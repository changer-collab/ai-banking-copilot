"""渠道可靠性辅助件：事件幂等、出站重试与健康统计。

本模块不参与身份、计划或策略裁决。事件台账只记录渠道和事件号，
健康统计只提供运行态指标，便于回调入口和健康页复用。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock
from typing import Any, TypeVar

from ai_banking_copilot.gateway.store import Store

T = TypeVar("T")

EVENT_LEDGER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS channel_events (
    channel    TEXT NOT NULL,
    event_id   TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (channel, event_id)
);
"""

DEFAULT_RETRY_DELAYS: tuple[float, ...] = (0.5, 2.0, 8.0)


class EventLedger:
    """SQLite 事件认领表；同一渠道的同一 event_id 只能成功认领一次。"""

    def __init__(self, store: Store, channel: str = "feishu") -> None:
        self._store = store
        self._channel = channel
        with self._store.transaction() as connection:
            connection.execute(EVENT_LEDGER_SCHEMA_SQL)

    def claim(self, event_id: str) -> bool:
        """原子认领事件；首次返回 True，重复或空事件号返回 False。"""
        if not event_id:
            return False
        claimed_at = datetime.now(UTC).isoformat()
        try:
            with self._store.transaction() as connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO channel_events(channel, event_id, claimed_at) "
                    "VALUES (?, ?, ?)",
                    (self._channel, event_id, claimed_at),
                )
                return cursor.rowcount == 1
        except sqlite3.Error:
            # 事件台账写失败必须 fail-closed，避免重复执行业务。
            return False

    def release(self, event_id: str) -> None:
        """撤销一次认领：只在本次请求**没有真正执行**时调用（401 / 429 / 状态机拒绝）。

        平台重投时必须还能再进来一次；成功执行过的事件不释放，保证幂等。
        """
        if not event_id:
            return
        try:
            with self._store.transaction() as connection:
                connection.execute(
                    "DELETE FROM channel_events WHERE channel = ? AND event_id = ?",
                    (self._channel, event_id),
                )
        except sqlite3.Error:
            # 释放失败只会导致该事件被判为重复（宁可少执行，不可重复执行）。
            return


def retry_outbound(
    send: Callable[[], T],
    *,
    attempts: int = 3,
    sleeper: Callable[[float], None] = time.sleep,
    delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
) -> T:
    """发送出站消息；仅对超时与 5xx 重试，最多调用 ``attempts`` 次。

    发送函数可以返回带 ``status_code`` 属性的响应对象，也可以返回整数状态码。
    4xx 或其他成功结果立即返回；最后一次 5xx 也原样返回，交由上层记录失败。
    """
    if attempts < 1:
        raise ValueError("attempts_must_be_positive")
    for index in range(attempts):
        try:
            result = send()
        except Exception as error:
            if not _retryable_exception(error) or index >= attempts - 1:
                raise
            _sleep_before_retry(index, delays, sleeper)
            continue

        if _status_code(result) >= 500 and index < attempts - 1:
            _sleep_before_retry(index, delays, sleeper)
            continue
        return result
    raise RuntimeError("outbound_retry_exhausted")


class HealthRegistry:
    """按渠道保存最近事件、失败、重试和限流计数。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._channels: dict[str, dict[str, object]] = {}

    def record_event(self, channel: str, *, success: bool) -> None:
        with self._lock:
            stats = self._stats(channel)
            self._increment(stats, "events")
            key = "ok" if success else "failed"
            self._increment(stats, key)
            stats["last_event_at"] = datetime.now(UTC).isoformat()
            stats["status"] = "ok" if success else "degraded"

    def record_replay(self, channel: str) -> None:
        with self._lock:
            stats = self._stats(channel)
            self._increment(stats, "replayed")

    def record_retry(self, channel: str) -> None:
        with self._lock:
            stats = self._stats(channel)
            self._increment(stats, "retries")

    def record_rate_limit(self, channel: str) -> None:
        with self._lock:
            stats = self._stats(channel)
            self._increment(stats, "ratelimit_hits")

    def snapshot(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {channel: dict(stats) for channel, stats in self._channels.items()}

    def _stats(self, channel: str) -> dict[str, object]:
        if channel not in self._channels:
            self._channels[channel] = {
                "status": "unknown",
                "last_event_at": None,
                "events": 0,
                "ok": 0,
                "failed": 0,
                "replayed": 0,
                "retries": 0,
                "ratelimit_hits": 0,
            }
        return self._channels[channel]

    @staticmethod
    def _increment(stats: dict[str, object], key: str) -> None:
        current = stats.get(key)
        stats[key] = current + 1 if isinstance(current, int) else 1


def _sleep_before_retry(
    index: int,
    delays: tuple[float, ...],
    sleeper: Callable[[float], None],
) -> None:
    if delays:
        sleeper(delays[min(index, len(delays) - 1)])


def _status_code(result: Any) -> int:
    if isinstance(result, int):
        return result
    try:
        return int(result.status_code)
    except (AttributeError, TypeError, ValueError):
        return 0


def _retryable_exception(error: Exception) -> bool:
    if isinstance(error, TimeoutError):
        return True
    error_type = type(error)
    return error_type.__module__ == "httpx" and error_type.__name__ == "TimeoutException"
