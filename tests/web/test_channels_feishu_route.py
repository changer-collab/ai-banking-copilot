from __future__ import annotations

import json
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app
from ai_banking_copilot.web.channels.feishu import FeishuAdapter, signature_for
from ai_banking_copilot.web.channels.identity import init_channel_schema


def _signed(body: bytes, key: str = "replay-key") -> dict[str, str]:
    timestamp, nonce = str(int(time.time())), "nonce"
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


def test_feishu_unbound_open_id_is_rejected_and_can_retry_after_binding(
    deps: AppDeps, rw_store, monkeypatch
) -> None:
    monkeypatch.setenv("AIB_FEISHU_ENCRYPT_KEY", "replay-key")
    monkeypatch.setenv("AIB_FEISHU_VERIFICATION_TOKEN", "verify-token")
    init_channel_schema(rw_store)
    client = TestClient(create_app(replace(deps, channel_store=rw_store)))
    body = json.dumps(_message("evt-unbound")).encode()

    rejected = client.post("/api/im/feishu", content=body, headers=_signed(body))

    assert rejected.status_code == 401
    assert rejected.json()["detail"] == "identity_unbound"
    rw_store.execute(
        "INSERT INTO channel_identities(channel, external_user_id, user_id, bound_at) "
        "VALUES ('feishu', 'ou-1', ?, 'now')",
        (deps.user_id,),
    )
    retried = client.post("/api/im/feishu", content=body, headers=_signed(body))

    assert retried.status_code == 200
    assert retried.json()["data"]["channel"] == "feishu"


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
    for index in range(25):
        body = json.dumps(_message(f"evt-burst-{index}")).encode()
        responses.append(client.post("/api/im/feishu", content=body, headers=_signed(body)))
    assert [response.status_code for response in responses] == [200] * 20 + [429] * 5
    assert all(response.headers["retry-after"] == "60" for response in responses[20:])
    health = client.get("/api/channels/health").json()
    assert health["health"]["feishu"]["ratelimit_hits"] == 5

    duplicate = json.dumps(_message("evt-burst-0")).encode()
    replayed = client.post(
        "/api/im/feishu", content=duplicate, headers=_signed(duplicate)
    )
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True

    retry = json.dumps(_message("evt-burst-20")).encode()
    retried = client.post("/api/im/feishu", content=retry, headers=_signed(retry))
    assert retried.status_code == 429


def test_live_feishu_preadmission_is_consumed_without_second_charge(
    deps: AppDeps, rw_store, monkeypatch
) -> None:
    monkeypatch.setenv("AIB_FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("AIB_FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("AIB_FEISHU_ENCRYPT_KEY", "live-key")
    monkeypatch.setenv("AIB_FEISHU_VERIFICATION_TOKEN", "verify-token")
    monkeypatch.delenv("AIB_FEISHU_REPLAY_MODE", raising=False)
    sent: list[str] = []

    def send(_adapter: FeishuAdapter, outbound) -> int:
        sent.append(outbound.chat_id)
        return 200

    monkeypatch.setattr(FeishuAdapter, "send", send)
    init_channel_schema(rw_store)
    rw_store.execute(
        "INSERT INTO channel_identities(channel, external_user_id, user_id, bound_at) "
        "VALUES ('feishu', 'ou-1', ?, 'now')",
        (deps.user_id,),
    )
    client = TestClient(create_app(replace(deps, channel_store=rw_store)))

    responses = []
    for index in range(25):
        body = json.dumps(_message(f"evt-live-{index}")).encode()
        responses.append(
            client.post("/api/im/feishu", content=body, headers=_signed(body, "live-key"))
        )

    assert [response.status_code for response in responses] == [200] * 20 + [429] * 5
    assert len(sent) == 20
    duplicate = json.dumps(_message("evt-live-0")).encode()
    replayed = client.post(
        "/api/im/feishu", content=duplicate, headers=_signed(duplicate, "live-key")
    )
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    assert len(sent) == 20
    health = client.get("/api/channels/health").json()["health"]["feishu"]
    assert health["ok"] == 20
    assert health["failed"] == 0
    assert health["ratelimit_hits"] == 5
    assert health["retries"] == 0
