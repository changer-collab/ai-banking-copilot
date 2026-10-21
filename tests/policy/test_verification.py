from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from ai_banking_copilot.policy.verification import (
    ChallengeRecord,
    DemoVerificationProvider,
    ExternalVerificationProvider,
    VerificationAttemptLimiter,
    VerificationBinding,
    VerificationError,
    create_challenge,
    invalidate_challenge,
    verify_challenge,
)


def binding() -> VerificationBinding:
    return VerificationBinding("u-1", "s-1", "e-1", "card-1", "hash-1")


def test_challenge_is_hmac_backed_and_one_time() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    record, code = create_challenge(binding(), key=b"test-secret", now=now)

    assert isinstance(record, ChallengeRecord)
    assert record.status == "active"
    assert len(code) == 6
    assert code not in record.code_digest
    consumed = verify_challenge(record, binding(), code, key=b"test-secret", now=now)
    with pytest.raises(VerificationError) as exc:
        verify_challenge(consumed, binding(), code, key=b"test-secret", now=now)
    assert exc.value.reason == "inactive"


def test_wrong_code_does_not_consume_and_binding_mismatch_is_rejected() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    record, code = create_challenge(binding(), key=b"test-secret", now=now)

    with pytest.raises(VerificationError) as exc:
        wrong = "000001" if code == "000000" else "000000"
        verify_challenge(record, binding(), wrong, key=b"test-secret", now=now)
    assert exc.value.reason == "invalid_code"
    assert verify_challenge(record, binding(), code, key=b"test-secret", now=now)

    record, code = create_challenge(binding(), key=b"test-secret", now=now)
    mismatched = VerificationBinding("u-2", "s-1", "e-1", "card-1", "hash-1")
    with pytest.raises(VerificationError) as exc:
        verify_challenge(record, mismatched, code, key=b"test-secret", now=now)
    assert exc.value.reason == "binding_mismatch"


def test_expired_challenge_and_rotation() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    first, first_code = create_challenge(binding(), key=b"test-secret", now=now)
    second, second_code = create_challenge(
        binding(), key=b"test-secret", now=now + timedelta(seconds=1), ttl_seconds=10
    )
    first = invalidate_challenge(first)
    with pytest.raises(VerificationError) as exc:
        verify_challenge(first, binding(), first_code, key=b"test-secret", now=now)
    assert exc.value.reason == "inactive"
    with pytest.raises(VerificationError) as exc:
        verify_challenge(
            second,
            binding(),
            second_code,
            key=b"test-secret",
            now=now + timedelta(seconds=11),
        )
    assert exc.value.reason == "expired"


def test_external_provider_posts_safe_payload_and_masks_target() -> None:
    calls: list[dict[str, object]] = []

    class Response:
        status_code = 202

        def json(self) -> dict[str, str]:
            return {"reference": "provider-ref"}

    class Transport:
        def post(self, endpoint: str, **kwargs: object) -> Response:
            calls.append({"endpoint": endpoint, **kwargs})
            return Response()

    provider = ExternalVerificationProvider(
        endpoint="https://verify.invalid/challenge",
        token="server-token",
        target="+8613800138000",
        transport=Transport(),
    )
    result = provider.deliver(
        binding(), "challenge-1", "123456", datetime(2026, 10, 20, tzinfo=UTC)
    )
    assert result.mode == "external"
    assert result.target.endswith("8000")
    assert "13800138000" not in result.target
    assert calls[0]["headers"] == {"Authorization": "Bearer server-token"}
    payload = calls[0]["json"]
    assert isinstance(payload, dict)
    assert payload["code"] == "123456"
    assert payload["target"] == "+8613800138000"
    assert payload["binding"] == {
        "user_id": "u-1",
        "session_id": "s-1",
        "execution_id": "e-1",
        "card_id": "card-1",
        "frozen_hash": "hash-1",
    }


@pytest.mark.parametrize(
    "provider",
    [
        ExternalVerificationProvider(
            endpoint=None, token="x", target="+8613800138000", transport=None
        ),
        ExternalVerificationProvider(
            endpoint="https://verify.invalid", token=None, target="+8613800138000", transport=None
        ),
        ExternalVerificationProvider(
            endpoint="https://verify.invalid", token="x", target=None, transport=None
        ),
    ],
)
def test_external_provider_fails_closed_without_configuration(
    provider: ExternalVerificationProvider,
) -> None:
    with pytest.raises(VerificationError) as exc:
        provider.deliver(binding(), "challenge-1", "123456", datetime(2026, 10, 20, tzinfo=UTC))
    assert exc.value.reason == "delivery_unavailable"


@pytest.mark.parametrize(
    "field", ["user_id", "session_id", "execution_id", "card_id", "frozen_hash"]
)
def test_every_binding_field_must_match(field: str) -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    record, code = create_challenge(binding(), key=b"secret", now=now)
    changed = replace(binding(), **{field: "changed"})
    with pytest.raises(VerificationError, match="^binding_mismatch$"):
        verify_challenge(record, changed, code, key=b"secret", now=now)


@pytest.mark.parametrize("wrong", [None, "", "not-a-code", "１２３４５６", "0000000"])
def test_missing_and_wrong_codes_do_not_consume(wrong: str | None) -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    record, code = create_challenge(binding(), key=b"secret", now=now)
    with pytest.raises(VerificationError, match="^invalid_code$"):
        verify_challenge(record, binding(), wrong, key=b"secret", now=now)
    consumed = verify_challenge(record, binding(), code, key=b"secret", now=now)
    assert consumed.status == "consumed"
    with pytest.raises(VerificationError, match="^inactive$"):
        verify_challenge(consumed, binding(), code, key=b"secret", now=now)


def test_records_are_frozen_and_invalidation_and_empty_key_fail_closed() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    record, code = create_challenge(binding(), key=b"secret", now=now)
    assert record.expires_at == now + timedelta(seconds=300)
    assert "code" not in asdict(record)
    with pytest.raises(FrozenInstanceError):
        record.status = "consumed"  # type: ignore[misc]
    with pytest.raises(VerificationError, match="^inactive$"):
        verify_challenge(invalidate_challenge(record), binding(), code, key=b"secret", now=now)
    with pytest.raises(VerificationError, match="^invalid_secret$"):
        create_challenge(binding(), key=b"", now=now)
    with pytest.raises(VerificationError, match="^invalid_code$"):
        verify_challenge(record, binding(), code, key=b"wrong-secret", now=now)


def test_demo_and_external_do_not_misrepresent_delivery() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    result = DemoVerificationProvider().deliver(binding(), "c1", "123456", now)
    assert result.mode == "demo" and result.channel == "demo"
    assert result.code == "123456"


@pytest.mark.parametrize("failure", ["non_2xx", "timeout", "invalid_json", "json_array"])
def test_external_fails_closed_without_retry_or_sensitive_error(failure: str) -> None:
    calls = 0
    secrets = "123456 server-token +8613800138000"

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx.ReadTimeout(secrets, request=request)
        if failure == "invalid_json":
            return httpx.Response(200, text=secrets)
        if failure == "json_array":
            return httpx.Response(200, json=[secrets])
        return httpx.Response(503, json={"error": secrets})

    provider = ExternalVerificationProvider(
        endpoint="https://verify.invalid/challenge",
        token="server-token",
        target="+8613800138000",
        transport=httpx.MockTransport(handle),
    )
    with pytest.raises(VerificationError) as exc:
        provider.deliver(binding(), "c1", "123456", datetime(2026, 10, 20, tzinfo=UTC))
    assert exc.value.reason == "delivery_failed"
    assert str(exc.value) == "delivery_failed"
    assert calls == 1


def test_external_injected_client_and_reference_are_safe() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    secrets = "123456 server-token +8613800138000"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"reference": secrets})
    )
    with httpx.Client(transport=transport) as client:
        provider = ExternalVerificationProvider(
            endpoint="https://verify.invalid/challenge",
            token="server-token",
            target="+8613800138000",
            client=client,
        )
        result = provider.deliver(binding(), "c1", "123456", now)
    assert result.mode == "external"
    assert result.code is None
    assert all(secret not in repr(result) for secret in secrets.split())


def test_external_default_client_sends_once_without_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructors: list[dict[str, object]] = []
    requests: list[httpx.Request] = []
    original_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, json={"delivered": True})

    def make_client(**kwargs: Any) -> httpx.Client:
        constructors.append(kwargs.copy())
        kwargs["transport"] = httpx.MockTransport(handle)
        return original_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", make_client)
    provider = ExternalVerificationProvider(
        endpoint="https://verify.invalid/challenge",
        token="server-token",
        target="configured-recipient",
        timeout=7.0,
    )
    result = provider.deliver(binding(), "c1", "123456", datetime(2026, 10, 20, tzinfo=UTC))
    assert result.reference is None
    assert len(requests) == 1
    assert constructors[0]["trust_env"] is False
    assert constructors[0]["timeout"] == 7.0


@pytest.mark.parametrize("field", ["delivered", "success"])
def test_external_explicit_rejection_is_failure(field: str) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={field: False, "reference": "not-delivered"})

    provider = ExternalVerificationProvider(
        endpoint="https://verify.invalid/challenge",
        token="server-token",
        target="configured-recipient",
        transport=httpx.MockTransport(handle),
    )
    with pytest.raises(VerificationError, match="^delivery_failed$"):
        provider.deliver(binding(), "c1", "123456", datetime(2026, 10, 20, tzinfo=UTC))
    assert calls == 1


@pytest.mark.parametrize("data", [{}, {"success": True}, {"delivered": True}])
def test_external_successful_json_dict_does_not_require_reference(data: dict[str, object]) -> None:
    provider = ExternalVerificationProvider(
        endpoint="https://verify.invalid/challenge",
        token="server-token",
        target="configured-recipient",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data)),
    )
    result = provider.deliver(binding(), "c1", "123456", datetime(2026, 10, 20, tzinfo=UTC))
    assert result.mode == "external"
    assert result.reference is None


def test_attempt_limiter_backs_off_then_locks_then_recovers() -> None:
    """失败计数只增不减（换挑战/换会话都不清零），退避逐级拉长，达上限后锁定并自动解锁。"""
    now = datetime(2026, 10, 20, tzinfo=UTC)
    limiter = VerificationAttemptLimiter(
        max_failures=3, lockout_seconds=60, backoff_seconds=(0, 10)
    )

    limiter.check("u-1", now=now)
    assert limiter.record_failure("u-1", now=now) == 1
    # 首档退避为 0：合法用户可立即重试一次。
    limiter.check("u-1", now=now)

    assert limiter.record_failure("u-1", now=now) == 2
    with pytest.raises(VerificationError, match="^throttled$"):
        limiter.check("u-1", now=now + timedelta(seconds=9))
    limiter.check("u-1", now=now + timedelta(seconds=10))

    # 超过退避序列长度时沿用最后一档，且第 3 次失败立即进入锁定（锁定 60 秒）。
    assert limiter.record_failure("u-1", now=now + timedelta(seconds=10)) == 3
    with pytest.raises(VerificationError, match="^locked$"):
        limiter.check("u-1", now=now + timedelta(seconds=10))
    with pytest.raises(VerificationError, match="^locked$"):
        limiter.check("u-1", now=now + timedelta(seconds=69))
    # 锁定期结束 ⇒ 清零重新计数
    limiter.check("u-1", now=now + timedelta(seconds=70))
    assert limiter.failures("u-1") == 0


def test_attempt_limiter_is_per_user_and_reset_by_server_success() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    limiter = VerificationAttemptLimiter(
        max_failures=2, lockout_seconds=60, backoff_seconds=(0,)
    )
    limiter.record_failure("u-1", now=now)
    limiter.record_failure("u-1", now=now)
    with pytest.raises(VerificationError, match="^locked$"):
        limiter.check("u-1", now=now)
    # 其他用户不受影响
    limiter.check("u-2", now=now)
    assert limiter.failures("u-2") == 0
    limiter.reset("u-1")
    limiter.check("u-1", now=now)
    assert limiter.failures("u-1") == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_failures": 0},
        {"lockout_seconds": 0},
        {"backoff_seconds": ()},
        {"backoff_seconds": (-1,)},
    ],
)
def test_attempt_limiter_rejects_invalid_configuration(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        VerificationAttemptLimiter(**kwargs)  # type: ignore[arg-type]


def test_attempt_limiter_errors_carry_no_sensitive_detail() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)
    limiter = VerificationAttemptLimiter(
        max_failures=1, lockout_seconds=60, backoff_seconds=(0,)
    )
    limiter.record_failure("u-1", now=now)
    with pytest.raises(VerificationError) as exc:
        limiter.check("u-1", now=now)
    assert exc.value.reason == "locked"
    assert str(exc.value) == "locked"
