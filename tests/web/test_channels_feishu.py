from __future__ import annotations

import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest

from ai_banking_copilot.web.channels.contracts import CHANNEL_FEISHU, OutboundMessage
from ai_banking_copilot.web.channels.feishu import (
    FeishuAdapter,
    FeishuConfig,
    FeishuProtocolError,
    encrypt_payload,
    signature_for,
)


def _headers(
    key: str, body: bytes, *, timestamp: str | None = None, nonce: str = "n"
) -> dict[str, str]:
    stamp = timestamp if timestamp is not None else str(int(time.time()))
    return {
        "X-Lark-Request-Timestamp": stamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": signature_for(key, stamp, nonce, body),
    }


def _message_payload() -> dict[str, object]:
    return {
        "schema": "2.0",
        "header": {
            "event_id": "evt-1",
            "event_type": "im.message.receive_v1",
            "token": "verify-token",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou-1"}},
            "message": {
                "chat_id": "oc-1",
                "message_type": "text",
                "content": json.dumps({"text": "查一下余额"}),
            },
        },
    }


def _live_adapter(*, clock=None) -> FeishuAdapter:
    config = FeishuConfig(
        app_id="app",
        app_secret="secret",
        verification_token="verify",
        encrypt_key="encrypt",
    )
    return FeishuAdapter(config, clock=clock) if clock is not None else FeishuAdapter(config)


def _outbound() -> OutboundMessage:
    return OutboundMessage(
        channel=CHANNEL_FEISHU,
        chat_id="oc-1",
        session_id="feishu:oc-1",
        state="awaiting_confirm",
        text="请确认",
        card={"card_id": "card-1", "frozen_hash": "a" * 64, "lines": []},
        level="YELLOW",
    )


def test_verify_accepts_and_rejects_feishu_signature() -> None:
    key = "encrypt-key"
    body = json.dumps(_message_payload()).encode()
    adapter = FeishuAdapter(FeishuConfig(encrypt_key=key, verification_token="verify-token"))

    assert adapter.verify(_headers(key, body), body) is True
    tampered = dict(_headers(key, body))
    tampered["X-Lark-Signature"] = "0" * 64
    assert adapter.verify(tampered, body) is False


def test_stale_timestamp_is_rejected_even_when_signature_matches() -> None:
    key = "encrypt-key"
    body = json.dumps(_message_payload()).encode()
    adapter = FeishuAdapter(FeishuConfig(encrypt_key=key, verification_token="verify-token"))

    stale = _headers(key, body, timestamp="1")
    assert adapter.verify(stale, body) is False
    assert adapter.verify(stale, body, now=1.0) is True


def test_parse_card_action_returns_confirm_command() -> None:
    adapter = FeishuAdapter(FeishuConfig(encrypt_key="encrypt-key"))
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": "evt-card",
            "event_type": "card.action.trigger",
            "token": "verify-token",
        },
        "event": {
            "operator": {"open_id": "ou-1"},
            "action": {"value": {"card_id": "card-1", "frozen_hash": "a" * 64}},
            "context": {"open_chat_id": "oc-1"},
        },
    }

    event = adapter.parse_event(payload)

    assert event.inbound is None
    assert event.command is not None
    assert event.command.card_id == "card-1"
    assert event.command.frozen_hash == "a" * 64
    assert event.command.external_user_id == "ou-1"
    assert event.command.external_chat_id == "oc-1"
    assert event.command.event_id == "evt-card"


def test_parse_card_action_requires_frozen_hash() -> None:
    adapter = FeishuAdapter(FeishuConfig(encrypt_key="encrypt-key"))
    payload = {
        "schema": "2.0",
        "header": {"event_id": "evt-card", "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": "ou-1"},
            "action": {"value": {"card_id": "card-1", "frozen_hash": "short"}},
        },
    }

    with pytest.raises(FeishuProtocolError, match="invalid_card_action"):
        adapter.parse_event(payload)


def test_missing_production_credentials_use_explicit_replay_key() -> None:
    config = FeishuConfig.from_env({"AIB_FEISHU_REPLAY_MODE": "1"})
    assert config.mode == "replay"
    assert config.configured is False
    assert config.encrypt_key == "replay-key"


def test_parse_message_returns_bound_inbound_message() -> None:
    adapter = FeishuAdapter(FeishuConfig(encrypt_key="encrypt-key"))

    inbound = adapter.parse(_message_payload())

    assert inbound.channel == CHANNEL_FEISHU
    assert inbound.external_user_id == "ou-1"
    assert inbound.external_chat_id == "oc-1"
    assert inbound.event_id == "evt-1"
    assert inbound.text == "查一下余额"


def test_parse_message_requires_event_id() -> None:
    adapter = FeishuAdapter(FeishuConfig(encrypt_key="encrypt-key"))
    payload = _message_payload()
    header = dict(payload["header"])  # type: ignore[arg-type]
    header.pop("event_id")
    payload["header"] = header

    with pytest.raises(FeishuProtocolError, match="event_id_missing"):
        adapter.parse(payload)


def test_parse_url_verification_returns_challenge_event() -> None:
    adapter = FeishuAdapter(FeishuConfig(verification_token="verify-token"))

    event = adapter.parse_event(
        {"type": "url_verification", "token": "verify-token", "challenge": "challenge-1"}
    )

    assert event.challenge == "challenge-1"
    assert event.inbound is None


def test_decode_encrypted_payload_round_trips() -> None:
    key = "encrypt-key"
    body = json.dumps({"encrypt": encrypt_payload(_message_payload(), key)}).encode()
    adapter = FeishuAdapter(FeishuConfig(encrypt_key=key))

    decoded = adapter.decode_body(body)

    assert decoded == _message_payload()


def test_decode_rejects_tampered_ciphertext() -> None:
    key = "encrypt-key"
    encrypted = encrypt_payload(_message_payload(), key)
    raw = bytearray(base64.b64decode(encrypted))
    raw[-1] ^= 1
    body = json.dumps({"encrypt": base64.b64encode(raw).decode()}).encode()
    adapter = FeishuAdapter(FeishuConfig(encrypt_key=key))

    with pytest.raises(FeishuProtocolError):
        adapter.decode_body(body)


def test_render_uses_existing_feishu_card_renderer() -> None:
    outbound = OutboundMessage(
        channel=CHANNEL_FEISHU,
        chat_id="oc-1",
        session_id="feishu:oc-1",
        state="awaiting_confirm",
        text="请确认",
        card={"lines": ["转账 500 元"], "requires": ["user_confirm"]},
        level="YELLOW",
    )

    rendered = FeishuAdapter(FeishuConfig()).render(outbound)

    assert rendered["header"]["template"] == "orange"
    assert rendered["elements"]


def test_live_send_retries_message_without_requesting_token_again(monkeypatch) -> None:
    from ai_banking_copilot.web.channels import feishu as module

    responses = iter(
        (
            (200, {"tenant_access_token": "t", "expire": 7200}),
            (500, {}),
            (500, {}),
            (500, {}),
            (200, {"code": 0}),
        )
    )
    calls: list[float] = []
    monkeypatch.setattr(module, "_post_json", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(module.time, "sleep", calls.append)
    adapter = _live_adapter()

    assert adapter.send(_outbound()) == 200
    assert calls == [0.5, 2.0, 8.0]


def test_live_send_rejects_success_http_with_missing_response_code(monkeypatch) -> None:
    from ai_banking_copilot.web.channels import feishu as module

    responses = iter(
        (
            (200, {"tenant_access_token": "t", "expire": 7200}),
            (200, {}),
        )
    )
    monkeypatch.setattr(module, "_post_json", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(module.time, "sleep", lambda _delay: None)

    with pytest.raises(FeishuProtocolError, match="feishu_message_send_failed"):
        _live_adapter().send(_outbound())


def test_live_send_reports_each_scheduled_retry(monkeypatch) -> None:
    from ai_banking_copilot.web.channels import feishu as module

    responses = iter(
        [(200, {"tenant_access_token": "t", "expire": 7200})]
        + [(503, {})] * 4
    )
    monkeypatch.setattr(module, "_post_json", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(module.time, "sleep", lambda _delay: None)
    adapter = _live_adapter()
    attempts: list[tuple[int, int | None, str | None]] = []
    retries: list[tuple[int, float]] = []

    with pytest.raises(FeishuProtocolError, match="feishu_message_send_failed"):
        adapter.send(
            _outbound(),
            on_attempt=lambda no, status, error: attempts.append((no, status, error)),
            on_retry=lambda attempt_no, delay: retries.append((attempt_no, delay)),
        )

    assert attempts == [(index, 503, None) for index in range(1, 5)]
    assert retries == [(1, 0.5), (2, 2.0), (3, 8.0)]


def test_live_send_callbacks_never_expose_error_text(monkeypatch) -> None:
    from ai_banking_copilot.web.channels import feishu as module

    responses = iter(
        [
            (200, {"tenant_access_token": "secret-token", "expire": 7200}),
            TimeoutError("request body and credentials must stay secret"),
            (200, {"code": 0}),
        ]
    )

    def post(*_args, **_kwargs):
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(module, "_post_json", post)
    monkeypatch.setattr(module.time, "sleep", lambda _delay: None)
    attempts: list[tuple[int, int | None, str | None]] = []
    adapter = _live_adapter()

    assert adapter.send(_outbound(), on_attempt=lambda *args: attempts.append(args)) == 200
    assert attempts == [(1, None, "TimeoutError"), (2, 200, None)]


def test_live_send_reuses_token_until_refresh_window(monkeypatch) -> None:
    from ai_banking_copilot.web.channels import feishu as module

    now = [1000.0]
    token_requests = 0

    def post(url, _payload, _headers):
        nonlocal token_requests
        if "/auth/" in url:
            token_requests += 1
            return 200, {"tenant_access_token": f"t-{token_requests}", "expire": 3600}
        return 200, {"code": 0}

    monkeypatch.setattr(module, "_post_json", post)
    adapter = _live_adapter(clock=lambda: now[0])

    assert adapter.send(_outbound()) == 200
    now[0] += 3299
    assert adapter.send(_outbound()) == 200
    now[0] += 1
    assert adapter.send(_outbound()) == 200
    assert token_requests == 2


@pytest.mark.parametrize("expire", [None, 299])
def test_live_send_uses_conservative_refresh_for_short_or_missing_expiry(
    monkeypatch, expire
) -> None:
    from ai_banking_copilot.web.channels import feishu as module

    token_requests = 0

    def post(url, _payload, _headers):
        nonlocal token_requests
        if "/auth/" in url:
            token_requests += 1
            body: dict[str, object] = {"tenant_access_token": f"t-{token_requests}"}
            if expire is not None:
                body["expire"] = expire
            return 200, body
        return 200, {"code": 0}

    monkeypatch.setattr(module, "_post_json", post)
    adapter = _live_adapter(clock=lambda: 1000.0)

    assert adapter.send(_outbound()) == 200
    assert adapter.send(_outbound()) == 200
    assert token_requests == 2


def test_concurrent_token_refresh_requests_token_once(monkeypatch) -> None:
    from ai_banking_copilot.web.channels import feishu as module

    start = Barrier(2)
    counter_lock = Lock()
    token_requests = 0

    def post(url, _payload, _headers):
        nonlocal token_requests
        if "/auth/" in url:
            with counter_lock:
                token_requests += 1
            time.sleep(0.02)
            return 200, {"tenant_access_token": "shared", "expire": 3600}
        return 200, {"code": 0}

    def send(adapter: FeishuAdapter) -> int:
        start.wait()
        return adapter.send(_outbound())

    monkeypatch.setattr(module, "_post_json", post)
    adapter = _live_adapter(clock=lambda: 1000.0)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: send(adapter), range(2)))

    assert results == [200, 200]
    assert token_requests == 1
