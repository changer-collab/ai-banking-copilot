"""渠道身份绑定：**外部身份 ≠ 服务端用户**，必须显式绑定后才放行。

三条纪律：

1. **fail-closed**：外部 `user_id` 未命中绑定表即 `ChannelUnauthorized`（401），
   **绝不**把外部 id 直接当成演示用户；
2. **绑定码一次性**：6 位数字、`BIND_CODE_TTL_SECONDS` 秒过期、错 `MAX_BIND_ATTEMPTS`
   次即作废（防在线爆破）；绑定码必须与**签发时的渠道 + 外部身份**一致才生效；
3. **只存绑定关系**：本模块不读账户、不读计划、不判级——身份层只回答「这个外部 id 是谁」。

两个实现共用同一套判定：`MemoryIdentityDirectory`（单进程演示 / 测试）与
`StoreIdentityDirectory`（SQLite 持久化，跨重启仍生效）。
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.web.channels.contracts import ChannelError, ChannelUnauthorized

BIND_CODE_TTL_SECONDS = 300
"""绑定码有效期（秒）：与计划卡 TTL 同量级，够用且不留长尾。"""

BIND_CODE_LENGTH = 6
"""绑定码长度：6 位数字即可，靠「错 5 次作废」而不是长度抗爆破。"""

MAX_BIND_ATTEMPTS = 5
"""单码最大错误次数：达到即作废该码（`BindAttemptsExceededError`）。"""

CHANNEL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS channel_identities (
    channel          TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    bound_at         TEXT NOT NULL,
    PRIMARY KEY (channel, external_user_id)
);

CREATE TABLE IF NOT EXISTS channel_bind_codes (
    code             TEXT PRIMARY KEY,
    channel          TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    issued_at        TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0
);
"""


class IdentityError(ChannelError):
    """身份层稳定错误基类。"""

    code = "identity_error"
    status_code = 400


class IdentityUnboundError(ChannelUnauthorized):
    """外部身份未绑定：**不放行到会话层**（401）。"""

    code = "identity_unbound"
    status_code = 401


class BindCodeInvalidError(IdentityError):
    """绑定码不存在或与签发对象不符（400）。"""

    code = "bind_code_invalid"
    status_code = 400


class BindCodeExpiredError(BindCodeInvalidError):
    """绑定码已过期（400）。"""

    code = "bind_code_expired"
    status_code = 400


class BindAttemptsExceededError(IdentityError):
    """绑定码错误次数用尽并已作废（429，语义上等同「请重新申请」）。"""

    code = "bind_attempts_exceeded"
    status_code = 429


@dataclass(frozen=True)
class Binding:
    """一条「外部身份 → 服务端用户」的绑定事实。"""

    channel: str
    external_user_id: str
    user_id: str
    bound_at: str


@dataclass(frozen=True)
class _CodeRecord:
    """绑定码记录（内存与持久化实现共用的判定输入）。"""

    code: str
    channel: str
    external_user_id: str
    user_id: str
    issued_at: datetime
    expires_at: datetime
    attempts: int = 0


@runtime_checkable
class IdentityDirectory(Protocol):
    """身份目录：解析绑定、签发绑定码、消费绑定码。"""

    def resolve(self, channel: str, external_user_id: str) -> Binding | None:
        """按渠道 + 外部 id 查绑定；未绑定返回 `None`（由调用方 fail-closed）。"""
        ...

    def issue_code(
        self,
        channel: str,
        external_user_id: str,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> str:
        """签发一次性绑定码；同一外部身份重复申请即覆盖旧码。"""
        ...

    def bind(
        self,
        code: str,
        *,
        channel: str,
        external_user_id: str,
        now: datetime | None = None,
    ) -> Binding:
        """消费绑定码并落绑定；过期 / 不匹配 / 次数用尽一律抛错。"""
        ...

    def binding_count(self) -> int:
        """绑定总数（健康页只读指标）。"""
        ...


def _clock(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _new_code() -> str:
    return f"{secrets.randbelow(10**BIND_CODE_LENGTH):0{BIND_CODE_LENGTH}d}"


def _check(
    record: _CodeRecord | None,
    *,
    channel: str,
    external_user_id: str,
    now: datetime,
) -> tuple[_CodeRecord, Binding]:
    """校验绑定码并给出「消费后的绑定」。

    返回值第一项是**需要落库的码记录**（错码时 `attempts + 1`），第二项是凭据不匹配时
    唯一可能成立的绑定；调用方据此决定「作废 / 落绑定」。不匹配一律抛错。
    """
    if record is None:
        raise BindCodeInvalidError("绑定码不存在")
    if now >= record.expires_at:
        raise BindCodeExpiredError("绑定码已过期，请重新申请")
    if record.channel != channel or record.external_user_id != external_user_id:
        raise BindCodeInvalidError("绑定码与当前外部身份不符")
    return record, Binding(
        channel=record.channel,
        external_user_id=record.external_user_id,
        user_id=record.user_id,
        bound_at=now.isoformat(),
    )


def _issue_record(
    channel: str,
    external_user_id: str,
    user_id: str,
    now: datetime,
    *,
    exclude: str | None = None,
) -> _CodeRecord:
    code = _new_code()
    while code == exclude:
        code = _new_code()
    return _CodeRecord(
        code=code,
        channel=channel,
        external_user_id=external_user_id,
        user_id=user_id,
        issued_at=now,
        expires_at=now + timedelta(seconds=BIND_CODE_TTL_SECONDS),
    )


class MemoryIdentityDirectory:
    """进程内身份目录：单进程演示与测试用；重启即失效（无持久化）。"""

    def __init__(self) -> None:
        self._bindings: dict[tuple[str, str], Binding] = {}
        self._codes: dict[str, _CodeRecord] = {}

    def resolve(self, channel: str, external_user_id: str) -> Binding | None:
        return self._bindings.get((channel, external_user_id))

    def issue_code(
        self,
        channel: str,
        external_user_id: str,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> str:
        record = _issue_record(channel, external_user_id, user_id, _clock(now))
        for key, existing in list(self._codes.items()):
            if existing.channel == channel and existing.external_user_id == external_user_id:
                del self._codes[key]
        self._codes[record.code] = record
        return record.code

    def bind(
        self,
        code: str,
        *,
        channel: str,
        external_user_id: str,
        now: datetime | None = None,
    ) -> Binding:
        moment = _clock(now)
        record = self._codes.get(code)
        try:
            record, binding = _check(
                record,
                channel=channel,
                external_user_id=external_user_id,
                now=moment,
            )
        except BindCodeExpiredError:
            self._codes.pop(code, None)
            raise
        except BindCodeInvalidError:
            if record is not None:
                bumped = replace(record, attempts=record.attempts + 1)
                if bumped.attempts >= MAX_BIND_ATTEMPTS:
                    del self._codes[code]
                    raise BindAttemptsExceededError("绑定码错误次数用尽，请重新申请") from None
                self._codes[code] = bumped
            raise
        del self._codes[code]
        self._bindings[(channel, external_user_id)] = binding
        return binding

    def binding_count(self) -> int:
        return len(self._bindings)


class StoreIdentityDirectory:
    """SQLite 身份目录：绑定与绑定码随库持久化，跨重启仍生效。"""

    def __init__(self, store: Store) -> None:
        self._store = store

    def resolve(self, channel: str, external_user_id: str) -> Binding | None:
        rows = self._store.query(
            "SELECT channel, external_user_id, user_id, bound_at FROM channel_identities"
            " WHERE channel = ? AND external_user_id = ?",
            (channel, external_user_id),
        )
        if not rows:
            return None
        row = rows[0]
        return Binding(
            channel=str(row["channel"]),
            external_user_id=str(row["external_user_id"]),
            user_id=str(row["user_id"]),
            bound_at=str(row["bound_at"]),
        )

    def issue_code(
        self,
        channel: str,
        external_user_id: str,
        user_id: str,
        *,
        now: datetime | None = None,
    ) -> str:
        record = _issue_record(channel, external_user_id, user_id, _clock(now))
        with self._store.transaction() as conn:
            conn.execute(
                "DELETE FROM channel_bind_codes WHERE channel = ? AND external_user_id = ?",
                (channel, external_user_id),
            )
            conn.execute(
                "INSERT INTO channel_bind_codes"
                " (code, channel, external_user_id, user_id, issued_at, expires_at, attempts)"
                " VALUES (?, ?, ?, ?, ?, ?, 0)",
                (
                    record.code,
                    record.channel,
                    record.external_user_id,
                    record.user_id,
                    record.issued_at.isoformat(),
                    record.expires_at.isoformat(),
                ),
            )
        return record.code

    def bind(
        self,
        code: str,
        *,
        channel: str,
        external_user_id: str,
        now: datetime | None = None,
    ) -> Binding:
        moment = _clock(now)
        rows = self._store.query(
            "SELECT code, channel, external_user_id, user_id, issued_at, expires_at, attempts"
            " FROM channel_bind_codes WHERE code = ?",
            (code,),
        )
        record = _record_from_row(rows[0]) if rows else None
        try:
            record, binding = _check(
                record,
                channel=channel,
                external_user_id=external_user_id,
                now=moment,
            )
        except BindCodeExpiredError:
            self._delete_code(code)
            raise
        except BindCodeInvalidError:
            if record is not None:
                attempts = record.attempts + 1
                if attempts >= MAX_BIND_ATTEMPTS:
                    self._delete_code(code)
                    raise BindAttemptsExceededError("绑定码错误次数用尽，请重新申请") from None
                with self._store.transaction() as conn:
                    conn.execute(
                        "UPDATE channel_bind_codes SET attempts = ? WHERE code = ?",
                        (attempts, code),
                    )
            raise
        with self._store.transaction() as conn:
            conn.execute("DELETE FROM channel_bind_codes WHERE code = ?", (code,))
            conn.execute(
                "INSERT INTO channel_identities"
                " (channel, external_user_id, user_id, bound_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(channel, external_user_id)"
                " DO UPDATE SET user_id = excluded.user_id, bound_at = excluded.bound_at",
                (binding.channel, binding.external_user_id, binding.user_id, binding.bound_at),
            )
        return binding

    def binding_count(self) -> int:
        rows = self._store.query("SELECT COUNT(*) AS n FROM channel_identities")
        return int(rows[0]["n"])

    def _delete_code(self, code: str) -> None:
        with self._store.transaction() as conn:
            conn.execute("DELETE FROM channel_bind_codes WHERE code = ?", (code,))


def _record_from_row(row: sqlite3.Row) -> _CodeRecord:
    return _CodeRecord(
        code=str(row["code"]),
        channel=str(row["channel"]),
        external_user_id=str(row["external_user_id"]),
        user_id=str(row["user_id"]),
        issued_at=datetime.fromisoformat(str(row["issued_at"])),
        expires_at=datetime.fromisoformat(str(row["expires_at"])),
        attempts=int(row["attempts"]),
    )


def init_channel_schema(store: Store) -> None:
    """建渠道身份表（幂等）：组合根在写连接上调用一次即可。"""
    with store.transaction() as conn:
        conn.executescript(CHANNEL_SCHEMA_SQL)
