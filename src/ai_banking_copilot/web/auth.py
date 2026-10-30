"""登录凭证：**只回答「这个请求是谁」**，不参与业务判定。

四条纪律：

1. **口令不落明文**：`hashlib.scrypt` + 每用户独立随机盐；比对用
   `hmac.compare_digest`，失败与不存在返回同一个错误码（不泄露账号是否存在）；
2. **会话只存哈希**：Cookie 里的是不透明随机 token，库里只留 `sha256(token)`；
3. **暴力破解可收敛**：连续 `LOGIN_MAX_ATTEMPTS` 次失败即锁定
   `LOCK_BASE_SECONDS` 秒；解锁后继续失败则每次锁定翻倍（上限 `LOCK_MAX_SECONDS`），
   成功即清零；
4. **短时票据**：删除类动作（解绑渠道 / 删除会话历史）需要 5 分钟内的二次验证票据，
   票据同样只存哈希且一次性消费。

身份解析的 fail-closed 语义放在调用方（`app.current_user`）：本模块只给「有 / 没有」。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ai_banking_copilot.gateway.store import Store

PASSWORD_SALT_BYTES = 16
"""盐长度（字节）。"""

PASSWORD_HASH_BYTES = 32
"""`scrypt` 输出长度（字节）。"""

SESSION_IDLE_SECONDS = 3600
"""会话空闲有效期（秒）：60 分钟无操作即失效。"""

SESSION_RENEW_SECONDS = 300
"""滑动续期阈值（秒）：剩余不足该值才写库，避免轮询把库写爆。"""

LOGIN_MAX_ATTEMPTS = 5
"""连续失败次数阈值：达到即锁定。"""

LOCK_BASE_SECONDS = 60
"""首次锁定时长（秒）。"""

LOCK_MAX_SECONDS = 300
"""锁定时长上限（秒）。"""

TICKET_TTL_SECONDS = 300
"""二次验证票据有效期（秒）。"""

TICKET_PURPOSE_UNBIND = "unbind_channel"
TICKET_PURPOSE_DELETE_CONVERSATION = "delete_conversation"

AUTH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS auth_users (
    username      TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    phone_tail    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    locked_until  TEXT,
    failed_count  INTEGER NOT NULL DEFAULT 0,
    lock_level    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    issued_at    TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT,
    user_agent   TEXT,
    client_ip    TEXT
);

CREATE TABLE IF NOT EXISTS auth_tickets (
    ticket_hash TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    purpose     TEXT NOT NULL,
    issued_at   TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    consumed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id, issued_at);
"""

DEMO_ACCOUNTS: tuple[tuple[str, str, str, str], ...] = (
    ("chenming", "u_demo", "陈明", "0086"),
    ("linjing", "u_lin", "林静", "3911"),
)
"""演示账号：`(username, user_id, display_name, phone_tail)`。"""


class AuthError(RuntimeError):
    """认证层稳定错误基类；`code` / `status_code` 供接口层直接映射。"""

    code = "auth_error"
    status_code = 400


class AuthInvalidCredentialsError(AuthError):
    """账号不存在或口令不符：**两者同一个错误**，不泄露账号存在性（401）。"""

    code = "auth_invalid_credentials"
    status_code = 401


class AuthLockedError(AuthError):
    """失败次数用尽，账号临时锁定（429）。"""

    code = "auth_locked"
    status_code = 429


class AuthRequiredError(AuthError):
    """未登录或会话已失效（401）。"""

    code = "auth_required"
    status_code = 401


class AuthTicketInvalidError(AuthError):
    """二次验证票据不存在 / 过期 / 已用 / 用途不符（403）。"""

    code = "auth_ticket_invalid"
    status_code = 403


@dataclass(frozen=True)
class AuthUser:
    """一个可登录用户。"""

    user_id: str
    username: str
    display_name: str
    phone_tail: str


@dataclass(frozen=True)
class AuthSession:
    """一次有效登录。`token` 只在签发时存在，不落库。"""

    token: str
    user_id: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class LoginRecord:
    """最近登录记录（个人中心展示用）。"""

    issued_at: datetime
    expires_at: datetime
    user_agent: str
    client_ip: str
    revoked: bool


@dataclass(frozen=True)
class LoginResult:
    """登录成功的返回：会话凭据 + 用户档案。"""

    session: AuthSession
    user: AuthUser


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_password(password: str, salt: str) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt.encode("utf-8"),
        n=2**14,
        r=8,
        p=1,
        dklen=PASSWORD_HASH_BYTES,
    )


def _new_salt() -> str:
    return secrets.token_hex(PASSWORD_SALT_BYTES)


def lock_seconds(level: int) -> int:
    """第 `level` 次锁定对应的秒数：60 / 120 / 240，上限 300。"""

    exponent = max(level - 1, 0)
    seconds: int = LOCK_BASE_SECONDS * (2**exponent)
    return min(seconds, LOCK_MAX_SECONDS)


class AuthService:
    """登录 / 登出 / 会话解析 / 二次验证票据。"""

    def __init__(
        self,
        store: Store,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    # ---------- 账号 ----------

    def ensure_demo_accounts(self, password: str) -> tuple[AuthUser, ...]:
        """幂等播种演示账号；已存在则不改口令（避免重启后口令漂移）。"""

        created: list[AuthUser] = []
        for username, user_id, display_name, phone_tail in DEMO_ACCOUNTS:
            if self.user_by_username(username) is not None:
                continue
            salt = _new_salt()
            digest = _hash_password(password, salt)
            with self._store.transaction() as conn:
                conn.execute(
                    "INSERT INTO auth_users"
                    " (username, user_id, password_hash, salt, display_name, phone_tail,"
                    "  created_at, locked_until, failed_count, lock_level)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, 0)",
                    (
                        username,
                        user_id,
                        digest.hex(),
                        salt,
                        display_name,
                        phone_tail,
                        self._clock().isoformat(),
                    ),
                )
            created.append(
                AuthUser(
                    user_id=user_id,
                    username=username,
                    display_name=display_name,
                    phone_tail=phone_tail,
                )
            )
        return tuple(created)

    def user_by_username(self, username: str) -> AuthUser | None:
        rows = self._store.query(
            "SELECT user_id, username, display_name, phone_tail FROM auth_users"
            " WHERE username = ?",
            (username,),
        )
        return _user_from_row(rows[0]) if rows else None

    def user_by_id(self, user_id: str) -> AuthUser | None:
        rows = self._store.query(
            "SELECT user_id, username, display_name, phone_tail FROM auth_users"
            " WHERE user_id = ?",
            (user_id,),
        )
        return _user_from_row(rows[0]) if rows else None

    def verify_password(self, user_id: str, password: str) -> bool:
        """校验口令但**不签发会话**：供删除类动作的二次验证使用。

        故意不计入登录失败锁定：此时用户已持有有效会话，计次等于给出「把自己锁死」的
        自伤手段；真正的暴力破解面在 `login`。
        """

        rows = self._store.query(
            "SELECT password_hash, salt FROM auth_users WHERE user_id = ?", (user_id,)
        )
        if not rows:
            return False
        row = rows[0]
        expected = bytes.fromhex(str(row["password_hash"]))
        actual = _hash_password(password, str(row["salt"]))
        return hmac.compare_digest(expected, actual)

    # ---------- 登录 / 登出 ----------

    def login(
        self,
        username: str,
        password: str,
        *,
        user_agent: str = "",
        client_ip: str = "",
        now: datetime | None = None,
    ) -> LoginResult:
        moment = _now(now)
        rows = self._store.query(
            "SELECT user_id, username, display_name, phone_tail, password_hash, salt,"
            "  locked_until, failed_count, lock_level"
            " FROM auth_users WHERE username = ?",
            (username,),
        )
        if not rows:
            # 不存在也做一次等价耗时计算，避免时序侧信道。
            _hash_password(password, "0" * (PASSWORD_SALT_BYTES * 2))
            raise AuthInvalidCredentialsError("账号或口令不正确")

        row = rows[0]
        locked_until = _parse_optional(row["locked_until"])
        if locked_until is not None and moment < locked_until:
            raise AuthLockedError(
                f"登录失败次数过多，请 {int((locked_until - moment).total_seconds())} 秒后再试"
            )

        expected = bytes.fromhex(str(row["password_hash"]))
        actual = _hash_password(password, str(row["salt"]))
        if not hmac.compare_digest(expected, actual):
            self._register_failure(str(row["username"]), moment)
            raise AuthInvalidCredentialsError("账号或口令不正确")

        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE auth_users SET failed_count = 0, lock_level = 0, locked_until = NULL"
                " WHERE username = ?",
                (str(row["username"]),),
            )

        token = _new_token()
        issued_at = moment
        expires_at = moment + timedelta(seconds=SESSION_IDLE_SECONDS)
        with self._store.transaction() as conn:
            conn.execute(
                "INSERT INTO auth_sessions"
                " (token_hash, user_id, issued_at, expires_at, revoked_at, user_agent, client_ip)"
                " VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (
                    _token_hash(token),
                    str(row["user_id"]),
                    issued_at.isoformat(),
                    expires_at.isoformat(),
                    user_agent,
                    client_ip,
                ),
            )
        return LoginResult(
            session=AuthSession(
                token=token,
                user_id=str(row["user_id"]),
                issued_at=issued_at,
                expires_at=expires_at,
            ),
            user=_user_from_row(row),
        )

    def logout(self, token: str, *, now: datetime | None = None) -> bool:
        """撤销会话；返回是否确有会话被撤销（已失效 token 不报错）。"""

        moment = _now(now)
        with self._store.transaction() as conn:
            cursor = conn.execute(
                "UPDATE auth_sessions SET revoked_at = ?"
                " WHERE token_hash = ? AND revoked_at IS NULL",
                (moment.isoformat(), _token_hash(token)),
            )
            return cursor.rowcount > 0

    def resolve(self, token: str, *, now: datetime | None = None) -> AuthSession | None:
        """解析 token；过期 / 撤销一律 `None`。有效且剩余不足阈值时滑动续期。"""

        if not token:
            return None
        moment = _now(now)
        rows = self._store.query(
            "SELECT user_id, issued_at, expires_at, revoked_at FROM auth_sessions"
            " WHERE token_hash = ?",
            (_token_hash(token),),
        )
        if not rows:
            return None
        row = rows[0]
        if row["revoked_at"] is not None:
            return None
        try:
            expires_at = datetime.fromisoformat(str(row["expires_at"]))
            issued_at = datetime.fromisoformat(str(row["issued_at"]))
        except (TypeError, ValueError):
            # 台账里的时间戳损坏（如 NULL）不能 500，按未登录处理（fail-closed）。
            return None
        if moment >= expires_at:
            return None
        renewed = expires_at
        if (expires_at - moment) < timedelta(seconds=SESSION_RENEW_SECONDS):
            renewed = moment + timedelta(seconds=SESSION_IDLE_SECONDS)
            with self._store.transaction() as conn:
                conn.execute(
                    "UPDATE auth_sessions SET expires_at = ? WHERE token_hash = ?",
                    (renewed.isoformat(), _token_hash(token)),
                )
        return AuthSession(
            token=token,
            user_id=str(row["user_id"]),
            issued_at=issued_at,
            expires_at=renewed,
        )

    def recent_logins(self, user_id: str, *, limit: int = 5) -> tuple[LoginRecord, ...]:
        rows = self._store.query(
            "SELECT issued_at, expires_at, user_agent, client_ip, revoked_at FROM auth_sessions"
            " WHERE user_id = ? ORDER BY issued_at DESC LIMIT ?",
            (user_id, limit),
        )
        return tuple(
            LoginRecord(
                issued_at=datetime.fromisoformat(str(row["issued_at"])),
                expires_at=datetime.fromisoformat(str(row["expires_at"])),
                user_agent=str(row["user_agent"] or ""),
                client_ip=str(row["client_ip"] or ""),
                revoked=row["revoked_at"] is not None,
            )
            for row in rows
        )

    # ---------- 二次验证票据 ----------

    def issue_ticket(
        self,
        user_id: str,
        purpose: str,
        *,
        now: datetime | None = None,
    ) -> str:
        moment = _now(now)
        ticket = _new_token()
        with self._store.transaction() as conn:
            conn.execute(
                "INSERT INTO auth_tickets"
                " (ticket_hash, user_id, purpose, issued_at, expires_at, consumed_at)"
                " VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    _token_hash(ticket),
                    user_id,
                    purpose,
                    moment.isoformat(),
                    (moment + timedelta(seconds=TICKET_TTL_SECONDS)).isoformat(),
                ),
            )
        return ticket

    def consume_ticket(
        self,
        user_id: str,
        purpose: str,
        ticket: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """消费票据；不匹配一律 `AuthTicketInvalidError`（不区分原因）。"""

        moment = _now(now)
        if not ticket:
            raise AuthTicketInvalidError("请先完成二次验证")
        rows = self._store.query(
            "SELECT user_id, purpose, expires_at, consumed_at FROM auth_tickets"
            " WHERE ticket_hash = ?",
            (_token_hash(ticket),),
        )
        if not rows:
            raise AuthTicketInvalidError("二次验证票据无效")
        row = rows[0]
        if (
            str(row["user_id"]) != user_id
            or str(row["purpose"]) != purpose
            or row["consumed_at"] is not None
            or moment >= datetime.fromisoformat(str(row["expires_at"]))
        ):
            raise AuthTicketInvalidError("二次验证票据已失效")
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE auth_tickets SET consumed_at = ? WHERE ticket_hash = ?",
                (moment.isoformat(), _token_hash(ticket)),
            )

    # ---------- 内部 ----------

    def _register_failure(self, username: str, moment: datetime) -> None:
        """记一次失败；达到阈值即锁定并把 `failed_count` 归零。"""

        rows = self._store.query(
            "SELECT failed_count, lock_level FROM auth_users WHERE username = ?", (username,)
        )
        if not rows:
            return
        failed = int(rows[0]["failed_count"]) + 1
        level = int(rows[0]["lock_level"])
        if failed < LOGIN_MAX_ATTEMPTS:
            with self._store.transaction() as conn:
                conn.execute(
                    "UPDATE auth_users SET failed_count = ? WHERE username = ?",
                    (failed, username),
                )
            return
        level += 1
        locked_until = moment + timedelta(seconds=lock_seconds(level))
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE auth_users SET failed_count = 0, lock_level = ?, locked_until = ?"
                " WHERE username = ?",
                (level, locked_until.isoformat(), username),
            )


def init_auth_schema(store: Store) -> None:
    """建登录凭证表（幂等）：组合根在写连接上调用一次即可。"""

    with store.transaction() as conn:
        conn.executescript(AUTH_SCHEMA_SQL)


def _user_from_row(row: sqlite3.Row) -> AuthUser:
    return AuthUser(
        user_id=str(row["user_id"]),
        username=str(row["username"]),
        display_name=str(row["display_name"]),
        phone_tail=str(row["phone_tail"]),
    )


def _parse_optional(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value)
    return datetime.fromisoformat(text) if text else None
