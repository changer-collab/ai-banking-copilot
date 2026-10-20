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
from typing import Any, Protocol, runtime_checkable

DEFAULT_TTL_SECONDS = 300


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
        }
    )

    def __init__(self, reason: str) -> None:
        safe_reason = reason if reason in self._REASONS else "verification_failed"
        self.reason = safe_reason
        super().__init__(safe_reason)


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
    "DEFAULT_TTL_SECONDS",
    "DemoVerificationProvider",
    "DeliveryResult",
    "ExternalVerificationProvider",
    "VerificationBinding",
    "VerificationError",
    "VerificationMode",
    "VerificationProvider",
    "create_challenge",
    "invalidate_challenge",
    "verify_challenge",
]
