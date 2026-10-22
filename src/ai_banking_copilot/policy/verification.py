"""服务端一次性强验证挑战及投递适配器。

验证码只在签发和投递的短暂调用栈中存在；挑战记录只保存 HMAC 摘要，避免
日志、持久化或 provider 引用意外暴露验证码。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Lock
from typing import Any, Protocol, runtime_checkable

DEFAULT_TTL_SECONDS = 300
DEFAULT_MAX_FAILURES = 5
"""同一用户允许的连续验证码错误次数上限；达到后进入锁定期。"""

DEFAULT_LOCKOUT_SECONDS = 300
"""达到失败上限后的锁定时长（秒）：与挑战 TTL 同量级，避免长时锁死合法用户。"""

DEFAULT_BACKOFF_SECONDS: tuple[int, ...] = (0, 5, 15, 60)
"""第 n 次失败后要求的**最短**重试间隔（秒）；超出序列长度按最后一档。"""


class VerificationMode(StrEnum):
    DEMO = "demo"
    EXTERNAL = "external"


class ChallengeStatus(StrEnum):
    ACTIVE = "active"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REPLACED = "replaced"


@dataclass(frozen=True, slots=True)
class VerificationBinding:
    user_id: str
    session_id: str
    execution_id: str
    card_id: str
    frozen_hash: str


@dataclass(frozen=True, slots=True)
class ChallengeRecord:
    challenge_id: str
    binding: VerificationBinding
    code_digest: str
    expires_at: datetime
    status: ChallengeStatus
    provider_reference: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    mode: VerificationMode
    channel: str
    target: str
    reference: str | None
    # 仅 demo 可回显；external 必须保持 None。
    code: str | None = None


class VerificationError(RuntimeError):
    """稳定、可对外展示的验证失败原因。"""

    _REASONS = frozenset(
        {
            "invalid_secret",
            "unknown_challenge",
            "binding_mismatch",
            "inactive",
            "expired",
            "invalid_code",
            "delivery_unavailable",
            "delivery_failed",
            "throttled",
            "locked",
        }
    )

    def __init__(self, reason: str) -> None:
        safe_reason = reason if reason in self._REASONS else "verification_failed"
        self.reason = safe_reason
        super().__init__(safe_reason)


@dataclass(slots=True)
class _AttemptState:
    failures: int = 0
    retry_at: datetime | None = None
    locked_until: datetime | None = None


class VerificationAttemptLimiter:
    """按用户统计强验证失败次数并施加退避（**进程内**，不是分布式限流）。

    作用域是**用户**而不是挑战或会话：重新申请验证码、更换 `session_id`、挑战过期或被替换
    都不会清零计数，因此无法靠「换一个挑战」无限重置失败次数。

    局限（不得外推）：状态只在本进程内存里——多 worker / 多实例各自独立计数，进程重启后
    清零；需要跨实例一致时必须外部化（本次不引入）。
    """

    def __init__(
        self,
        *,
        max_failures: int = DEFAULT_MAX_FAILURES,
        lockout_seconds: int = DEFAULT_LOCKOUT_SECONDS,
        backoff_seconds: tuple[int, ...] = DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        if max_failures < 1:
            raise ValueError("max_failures 必须为正数")
        if lockout_seconds <= 0:
            raise ValueError("lockout_seconds 必须为正数")
        if not backoff_seconds or any(item < 0 for item in backoff_seconds):
            raise ValueError("backoff_seconds 必须为非负数序列")
        self._max_failures = max_failures
        self._lockout = timedelta(seconds=lockout_seconds)
        self._backoff = tuple(timedelta(seconds=item) for item in backoff_seconds)
        self._states: dict[str, _AttemptState] = {}
        self._lock = Lock()

    def check(self, user_id: str, *, now: datetime) -> None:
        """比较验证码**之前**调用：锁定期或退避中直接拒绝，不做摘要计算。"""
        moment = _as_utc(now)
        with self._lock:
            state = self._states.get(user_id)
            if state is None:
                return
            if state.locked_until is not None:
                if moment < state.locked_until:
                    raise VerificationError("locked")
                # 锁定期已过：开启新一轮计数，旧失败次数不再累积。
                self._states.pop(user_id, None)
                return
            if state.retry_at is not None and moment < state.retry_at:
                raise VerificationError("throttled")
            state.retry_at = None

    def record_failure(self, user_id: str, *, now: datetime) -> int:
        """记一次验证码错误，返回累计失败次数；达到上限时同时开启锁定期。"""
        moment = _as_utc(now)
        with self._lock:
            state = self._states.setdefault(user_id, _AttemptState())
            state.failures += 1
            if state.failures >= self._max_failures:
                state.locked_until = moment + self._lockout
                state.retry_at = None
            else:
                index = min(state.failures - 1, len(self._backoff) - 1)
                delay = self._backoff[index]
                state.retry_at = moment + delay if delay else None
            return state.failures

    def reset(self, user_id: str) -> None:
        """验证成功后清零；只有服务端确认的正确码会走到这里。"""
        with self._lock:
            self._states.pop(user_id, None)

    def failures(self, user_id: str) -> int:
        """当前累计失败次数（供审计与测试核对，不对外暴露验证信息）。"""
        with self._lock:
            state = self._states.get(user_id)
            return 0 if state is None else state.failures


@runtime_checkable
class VerificationProvider(Protocol):
    def deliver(
        self,
        binding: VerificationBinding,
        challenge_id: str,
        code: str,
        expires_at: datetime,
    ) -> DeliveryResult:
        """向配置的第二信道投递验证码。"""


class DemoVerificationProvider:
    """本地演示投递器；它明确标记为 demo，不代表真实短信下发。"""

    mode = VerificationMode.DEMO

    def __init__(self, *, target: str = "演示目标", channel: str = "demo") -> None:
        self.target = target
        self.channel = channel

    def deliver(
        self,
        binding: VerificationBinding,
        challenge_id: str,
        code: str,
        expires_at: datetime,
    ) -> DeliveryResult:
        del binding, expires_at
        return DeliveryResult(
            mode=self.mode,
            channel=self.channel,
            target=_mask_target(self.target),
            reference=f"demo:{challenge_id}",
            code=code,
        )


class ExternalVerificationProvider:
    """通用 HTTP JSON 投递器。

    `transport` 可为具有 ``post`` 方法的测试 sender，也可为 httpx transport。
    """

    mode = VerificationMode.EXTERNAL

    def __init__(
        self,
        *,
        endpoint: str | None,
        token: str | None,
        target: str | None,
        client: Any | None = None,
        transport: Any | None = None,
        timeout: float = 10.0,
        channel: str = "external",
    ) -> None:
        self.endpoint = endpoint
        self.token = token
        self.target = target
        self.client = client
        self.transport = transport
        self.timeout = timeout
        self.channel = channel

    def deliver(
        self,
        binding: VerificationBinding,
        challenge_id: str,
        code: str,
        expires_at: datetime,
    ) -> DeliveryResult:
        if not self.endpoint or not self.token or not self.target:
            raise VerificationError("delivery_unavailable")
        payload: dict[str, object] = {
            "challenge_id": challenge_id,
            "binding": {
                "user_id": binding.user_id,
                "session_id": binding.session_id,
                "execution_id": binding.execution_id,
                "card_id": binding.card_id,
                "frozen_hash": binding.frozen_hash,
            },
            "code": code,
            "expires_at": _as_utc(expires_at).isoformat(),
            "target": self.target,
        }
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            response = self._post(payload, headers)
            status_code = int(getattr(response, "status_code", 0))
            if not 200 <= status_code < 300:
                raise VerificationError("delivery_failed")
            data = response.json()
            if not isinstance(data, dict):
                raise VerificationError("delivery_failed")
            if data.get("delivered") is False or data.get("success") is False:
                raise VerificationError("delivery_failed")
            raw_reference = data.get("reference")
            reference = _mask_reference(raw_reference) if isinstance(raw_reference, str) else None
        except VerificationError:
            raise
        except Exception as error:
            del error
            raise VerificationError("delivery_failed") from None
        return DeliveryResult(
            mode=self.mode,
            channel=self.channel,
            target=_mask_target(self.target),
            reference=reference,
        )

    def _post(self, payload: dict[str, object], headers: dict[str, str]) -> Any:
        sender = self.client if self.client is not None else self.transport
        post = getattr(sender, "post", None)
        if callable(post):
            return post(self.endpoint, json=payload, headers=headers, timeout=self.timeout)
        # 接受 httpx transport，但不把 httpx 变为核心模块的强依赖。
        try:
            import httpx
        except ImportError:
            raise VerificationError("delivery_unavailable") from None
        endpoint = self.endpoint
        if endpoint is None:
            raise VerificationError("delivery_unavailable")
        with httpx.Client(transport=sender, timeout=self.timeout, trust_env=False) as client:
            return client.post(endpoint, json=payload, headers=headers)


def create_challenge(
    binding: VerificationBinding,
    *,
    key: bytes,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> tuple[ChallengeRecord, str]:
    """创建不含明文验证码的挑战记录，同时返回仅供投递器使用的验证码。"""
    _validate_key(key)
    lifetime = timedelta(seconds=ttl_seconds)
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds 必须为正数")
    issued_at = _as_utc(now or datetime.now(UTC))
    challenge_id = secrets.token_urlsafe(18)
    code = f"{secrets.randbelow(1_000_000):06d}"
    digest = _code_digest(key, challenge_id, binding, code)
    return (
        ChallengeRecord(
            challenge_id=challenge_id,
            binding=binding,
            code_digest=digest,
            expires_at=issued_at + lifetime,
            status=ChallengeStatus.ACTIVE,
        ),
        code,
    )


def verify_challenge(
    record: ChallengeRecord,
    binding: VerificationBinding,
    code: str | None,
    *,
    key: bytes,
    now: datetime | None = None,
) -> ChallengeRecord:
    """校验挑战并返回一次性消费后的不可变记录。"""
    _validate_key(key)
    if record.status != ChallengeStatus.ACTIVE:
        raise VerificationError("inactive")
    if record.binding != binding:
        raise VerificationError("binding_mismatch")
    if _as_utc(now or datetime.now(UTC)) >= _as_utc(record.expires_at):
        raise VerificationError("expired")
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
        raise VerificationError("invalid_code")
    expected = _code_digest(key, record.challenge_id, binding, code)
    if not hmac.compare_digest(expected, record.code_digest):
        raise VerificationError("invalid_code")
    return replace(record, status=ChallengeStatus.CONSUMED)


def invalidate_challenge(record: ChallengeRecord) -> ChallengeRecord:
    """使挑战失效，供重新申请或会话重置调用。"""
    if record.status != ChallengeStatus.ACTIVE:
        return record
    return replace(record, status=ChallengeStatus.REPLACED)


def _code_digest(
    key: bytes,
    challenge_id: str,
    binding: VerificationBinding,
    code: str,
) -> str:
    message = "\x1f".join(
        (challenge_id, binding.user_id, binding.session_id, binding.execution_id,
         binding.card_id, binding.frozen_hash, code)
    ).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _validate_key(key: bytes) -> None:
    if not isinstance(key, bytes) or not key:
        raise VerificationError("invalid_secret")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _mask_target(target: str) -> str:
    if len(target) <= 4:
        return "*" * len(target)
    return "*" * max(4, len(target) - 4) + target[-4:]


def _mask_reference(reference: str) -> str:
    # Provider 可能把收件人或正文当作引用返回；只返回不可逆的关联标签。
    return f"ref:{hashlib.sha256(reference.encode('utf-8')).hexdigest()[:16]}"


__all__ = [
    "ChallengeRecord",
    "ChallengeStatus",
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_LOCKOUT_SECONDS",
    "DEFAULT_MAX_FAILURES",
    "DEFAULT_TTL_SECONDS",
    "DemoVerificationProvider",
    "DeliveryResult",
    "ExternalVerificationProvider",
    "VerificationAttemptLimiter",
    "VerificationBinding",
    "VerificationError",
    "VerificationMode",
    "VerificationProvider",
    "create_challenge",
    "invalidate_challenge",
    "verify_challenge",
]
