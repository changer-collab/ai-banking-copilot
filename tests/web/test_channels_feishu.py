from __future__ import annotations

import base64
import json
import time

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


def test_live_send_retries_server_errors_before_sending_card(monkeypatch) -> None:
    from ai_banking_copilot.web.channels import feishu as module

    responses = iter(
        ((500, {}), (500, {}), (200, {"tenant_access_token": "t"}), (200, {"code": 0}))
    )
    calls: list[float] = []
    monkeypatch.setattr(module, "_post_json", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(module.time, "sleep", calls.append)
    adapter = FeishuAdapter(
        FeishuConfig(
            app_id="app",
            app_secret="secret",
            verification_token="verify",
            encrypt_key="encrypt",
        )
    )
    outbound = OutboundMessage(
        channel=CHANNEL_FEISHU,
        chat_id="oc-1",
        session_id="feishu:oc-1",
        state="awaiting_confirm",
        text="请确认",
        card={"card_id": "card-1", "frozen_hash": "a" * 64, "lines": []},
        level="YELLOW",
    )

    assert adapter.send(outbound) == 200
    assert calls == [0.5, 2.0]
