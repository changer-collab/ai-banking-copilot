from __future__ import annotations

import json
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app
from ai_banking_copilot.web.channels.dispatcher import Dispatcher
from ai_banking_copilot.web.channels.feishu import FeishuAdapter, signature_for
from ai_banking_copilot.web.channels.identity import init_channel_schema
from ai_banking_copilot.web.session import Session


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


def _confirm_action(
    event_id: str, *, card_id: str, frozen_hash: str
) -> dict[str, object]:
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "card.action.trigger",
            "token": "verify-token",
        },
        "event": {
            "operator": {"open_id": "ou-1"},
            "action": {
                "value": {"card_id": card_id, "frozen_hash": frozen_hash}
            },
            "context": {"open_chat_id": "oc-1"},
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

    def send(_adapter: FeishuAdapter, outbound, *, on_retry=None) -> int:
        del on_retry
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


def test_live_feishu_send_exception_keeps_claim_and_records_one_failure(
    deps: AppDeps, rw_store, monkeypatch
) -> None:
    monkeypatch.setenv("AIB_FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("AIB_FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("AIB_FEISHU_ENCRYPT_KEY", "live-key")
    monkeypatch.setenv("AIB_FEISHU_VERIFICATION_TOKEN", "verify-token")
    monkeypatch.delenv("AIB_FEISHU_REPLAY_MODE", raising=False)
    dispatch_calls = 0
    original_dispatch = Dispatcher.dispatch_live

    def dispatch(dispatcher: Dispatcher, material):
        nonlocal dispatch_calls
        dispatch_calls += 1
        return original_dispatch(dispatcher, material)

    def send(_adapter: FeishuAdapter, _outbound, *, on_retry=None) -> int:
        del on_retry
        raise TimeoutError("network result unknown")

    monkeypatch.setattr(Dispatcher, "dispatch_live", dispatch)
    monkeypatch.setattr(FeishuAdapter, "send", send)
    init_channel_schema(rw_store)
    rw_store.execute(
        "INSERT INTO channel_identities(channel, external_user_id, user_id, bound_at) "
        "VALUES ('feishu', 'ou-1', ?, 'now')",
        (deps.user_id,),
    )
    client = TestClient(create_app(replace(deps, channel_store=rw_store)))
    body = json.dumps(_message("evt-send-exception")).encode()

    first = client.post("/api/im/feishu", content=body, headers=_signed(body, "live-key"))
    duplicate = client.post(
        "/api/im/feishu", content=body, headers=_signed(body, "live-key")
    )

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json()["replayed"] is True
    assert dispatch_calls == 1
    health = client.get("/api/channels/health").json()["health"]["feishu"]
    assert health["failed"] == 1
    assert health["ok"] == 0


def test_live_feishu_final_5xx_keeps_claim_and_records_one_failure(
    deps: AppDeps, rw_store, monkeypatch
) -> None:
    monkeypatch.setenv("AIB_FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("AIB_FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("AIB_FEISHU_ENCRYPT_KEY", "live-key")
    monkeypatch.setenv("AIB_FEISHU_VERIFICATION_TOKEN", "verify-token")
    monkeypatch.delenv("AIB_FEISHU_REPLAY_MODE", raising=False)
    dispatch_calls = 0
    original_dispatch = Dispatcher.dispatch_live

    def dispatch(dispatcher: Dispatcher, material):
        nonlocal dispatch_calls
        dispatch_calls += 1
        return original_dispatch(dispatcher, material)

    def send(_adapter: FeishuAdapter, _outbound, *, on_retry=None) -> int:
        del on_retry
        return 503

    monkeypatch.setattr(Dispatcher, "dispatch_live", dispatch)
    monkeypatch.setattr(FeishuAdapter, "send", send)
    init_channel_schema(rw_store)
    rw_store.execute(
        "INSERT INTO channel_identities(channel, external_user_id, user_id, bound_at) "
        "VALUES ('feishu', 'ou-1', ?, 'now')",
        (deps.user_id,),
    )
    client = TestClient(create_app(replace(deps, channel_store=rw_store)))
    body = json.dumps(_message("evt-send-5xx")).encode()

    client.post("/api/im/feishu", content=body, headers=_signed(body, "live-key"))
    duplicate = client.post(
        "/api/im/feishu", content=body, headers=_signed(body, "live-key")
    )

    assert duplicate.json()["replayed"] is True
    assert dispatch_calls == 1
    health = client.get("/api/channels/health").json()["health"]["feishu"]
    assert health["failed"] == 1
    assert health["ok"] == 0


def test_live_feishu_preadmission_failure_releases_claim_for_retry(
    deps: AppDeps, rw_store, monkeypatch
) -> None:
    monkeypatch.setenv("AIB_FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("AIB_FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("AIB_FEISHU_ENCRYPT_KEY", "live-key")
    monkeypatch.setenv("AIB_FEISHU_VERIFICATION_TOKEN", "verify-token")
    monkeypatch.delenv("AIB_FEISHU_REPLAY_MODE", raising=False)
    dispatch_calls = 0

    def resolve_user(_dispatcher: Dispatcher, _material):
        nonlocal dispatch_calls
        dispatch_calls += 1
        raise RuntimeError("identity lookup failed")

    def send(_adapter: FeishuAdapter, _outbound, *, on_retry=None) -> int:
        del on_retry
        raise AssertionError("send must not run")

    monkeypatch.setattr(Dispatcher, "_resolve_user", resolve_user)
    monkeypatch.setattr(FeishuAdapter, "send", send)
    init_channel_schema(rw_store)
    rw_store.execute(
        "INSERT INTO channel_identities(channel, external_user_id, user_id, bound_at) "
        "VALUES ('feishu', 'ou-1', ?, 'now')",
        (deps.user_id,),
    )
    client = TestClient(create_app(replace(deps, channel_store=rw_store)))
    body = json.dumps(_message("evt-dispatch-failure")).encode()

    first = client.post("/api/im/feishu", content=body, headers=_signed(body, "live-key"))
    retried = client.post(
        "/api/im/feishu", content=body, headers=_signed(body, "live-key")
    )

    assert first.status_code == 200
    assert retried.status_code == 200
    assert retried.json()["replayed"] is False
    assert dispatch_calls == 2
    health = client.get("/api/channels/health").json()["health"]["feishu"]
    assert health["failed"] == 2
    assert health["ok"] == 0


def test_live_feishu_confirm_audit_failure_keeps_claim_after_business_success(
    deps: AppDeps, rw_store, monkeypatch
) -> None:
    monkeypatch.setenv("AIB_FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("AIB_FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("AIB_FEISHU_ENCRYPT_KEY", "live-key")
    monkeypatch.setenv("AIB_FEISHU_VERIFICATION_TOKEN", "verify-token")
    monkeypatch.delenv("AIB_FEISHU_REPLAY_MODE", raising=False)
    init_channel_schema(rw_store)
    rw_store.execute(
        "INSERT INTO channel_identities(channel, external_user_id, user_id, bound_at) "
        "VALUES ('feishu', 'ou-1', ?, 'now')",
        (deps.user_id,),
    )
    client = TestClient(create_app(replace(deps, channel_store=rw_store)))
    submitted = client.post(
        "/api/submit",
        json={"utterance": "转给李娜 500 元", "session_id": "feishu:oc-1"},
    ).json()
    card = submitted["card"]
    confirm_calls = 0
    original_confirm = Session.confirm

    def confirm(session: Session, *args, **kwargs):
        nonlocal confirm_calls
        confirm_calls += 1
        return original_confirm(session, *args, **kwargs)

    def fail_audit(_dispatcher: Dispatcher, _material, _outbound) -> None:
        raise RuntimeError("channel audit unavailable")

    def send(_adapter: FeishuAdapter, _outbound, *, on_retry=None) -> int:
        del on_retry
        raise AssertionError("send must not run after dispatch failure")

    monkeypatch.setattr(Session, "confirm", confirm)
    monkeypatch.setattr(Dispatcher, "_record_audit", fail_audit)
    monkeypatch.setattr(FeishuAdapter, "send", send)
    body = json.dumps(
        _confirm_action(
            "evt-confirm-audit-failure",
            card_id=card["card_id"],
            frozen_hash=card["frozen_hash"],
        )
    ).encode()

    first = client.post("/api/im/feishu", content=body, headers=_signed(body, "live-key"))
    duplicate = client.post(
        "/api/im/feishu", content=body, headers=_signed(body, "live-key")
    )

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json()["replayed"] is True
    assert confirm_calls == 1
    health = client.get("/api/channels/health").json()["health"]["feishu"]
    assert health["failed"] == 1
    assert health["ok"] == 0


def test_live_feishu_records_each_scheduled_retry(
    deps: AppDeps, rw_store, monkeypatch
) -> None:
    monkeypatch.setenv("AIB_FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("AIB_FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("AIB_FEISHU_ENCRYPT_KEY", "live-key")
    monkeypatch.setenv("AIB_FEISHU_VERIFICATION_TOKEN", "verify-token")
    monkeypatch.delenv("AIB_FEISHU_REPLAY_MODE", raising=False)

    def send(_adapter: FeishuAdapter, _outbound, *, on_retry=None) -> int:
        assert on_retry is not None
        on_retry(1, 0.5)
        on_retry(2, 2.0)
        return 200

    monkeypatch.setattr(FeishuAdapter, "send", send)
    init_channel_schema(rw_store)
    rw_store.execute(
        "INSERT INTO channel_identities(channel, external_user_id, user_id, bound_at) "
        "VALUES ('feishu', 'ou-1', ?, 'now')",
        (deps.user_id,),
    )
    client = TestClient(create_app(replace(deps, channel_store=rw_store)))
    body = json.dumps(_message("evt-retries")).encode()

    client.post("/api/im/feishu", content=body, headers=_signed(body, "live-key"))

    health = client.get("/api/channels/health").json()["health"]["feishu"]
    assert health["ok"] == 1
    assert health["failed"] == 0
    assert health["retries"] == 2
