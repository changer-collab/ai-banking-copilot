from __future__ import annotations

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app
from ai_banking_copilot.web.channels.feishu import signature_for
from ai_banking_copilot.web.channels.identity import init_channel_schema


def _signed(body: bytes, key: str = "replay-key") -> dict[str, str]:
    timestamp, nonce = "1", "nonce"
    return {
        "X-Lark-Request-Timestamp": timestamp,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": signature_for(key, timestamp, nonce, body),
    }


def _message(event_id: str) -> dict[str, object]:
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
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


def test_feishu_url_verification_and_tamper_are_protocol_responses(
    deps: AppDeps, monkeypatch
) -> None:
    monkeypatch.setenv("AIB_FEISHU_ENCRYPT_KEY", "replay-key")
    monkeypatch.setenv("AIB_FEISHU_VERIFICATION_TOKEN", "verify-token")
    client = TestClient(create_app(deps))
    body = json.dumps(
        {"type": "url_verification", "token": "verify-token", "challenge": "c-1"}
    ).encode()

    response = client.post("/api/im/feishu", content=body, headers=_signed(body))
    assert response.status_code == 200
    assert response.json() == {"challenge": "c-1"}

    tampered = _signed(body)
    tampered["X-Lark-Signature"] = "0" * 64
    rejected = client.post("/api/im/feishu", content=body, headers=tampered)
    assert rejected.status_code == 401
    assert rejected.json()["detail"] == "signature_invalid"


def test_feishu_event_is_dispatched_once_and_health_is_adapter(
    deps: AppDeps, rw_store, monkeypatch
) -> None:
    monkeypatch.setenv("AIB_FEISHU_ENCRYPT_KEY", "replay-key")
    monkeypatch.setenv("AIB_FEISHU_VERIFICATION_TOKEN", "verify-token")
    init_channel_schema(rw_store)
    rw_store.execute(
        "INSERT INTO channel_identities(channel, external_user_id, user_id, bound_at) "
        "VALUES ('feishu', 'ou-1', ?, 'now')",
        (deps.user_id,),
    )
    client = TestClient(create_app(replace(deps, channel_store=rw_store)))
    body = json.dumps(_message("evt-once")).encode()

    first = client.post("/api/im/feishu", content=body, headers=_signed(body))
    second = client.post("/api/im/feishu", content=body, headers=_signed(body))

    assert first.status_code == 200
    assert first.json()["data"]["channel"] == "feishu"
    assert first.json()["data"]["rendered"]["feishu"]["elements"]
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert (
        second.json()["data"]["card"]["frozen_hash"]
        == first.json()["data"]["card"]["frozen_hash"]
    )
    health = client.get("/api/channels/health").json()
    assert health["channels"]["feishu"]["mode"] == "replay"
    assert health["health"]["feishu"]["replayed"] == 1


def test_feishu_burst_returns_retry_after(deps: AppDeps, rw_store, monkeypatch) -> None:
    monkeypatch.setenv("AIB_FEISHU_ENCRYPT_KEY", "replay-key")
    monkeypatch.setenv("AIB_FEISHU_VERIFICATION_TOKEN", "verify-token")
    init_channel_schema(rw_store)
    rw_store.execute(
        "INSERT INTO channel_identities(channel, external_user_id, user_id, bound_at) "
        "VALUES ('feishu', 'ou-1', ?, 'now')",
        (deps.user_id,),
    )
    client = TestClient(create_app(replace(deps, channel_store=rw_store)))
    responses = []
    for index in range(21):
        body = json.dumps(_message(f"evt-burst-{index}")).encode()
        responses.append(client.post("/api/im/feishu", content=body, headers=_signed(body)))
    assert responses[-1].status_code == 429
    assert responses[-1].headers["retry-after"] == "60"
