"""P0-4 渠道内核契约：IM 与 Web 走**同一套**会话状态机，且身份 / 幂等 / 审计纪律成立。

覆盖：
- 同一句话在 IM 与 Web 得到**同一状态与同一等级**（渠道只搬消息，不二次裁决）；
- 外部身份未绑定 ⇒ 401 `identity_unbound`（fail-closed）；
- 同一 `event_id` 第二次直接回放（`replayed=True`）；
- 审计带 `channel` 且只落摘要；
- `/api/im/confirm` 能真正完成执行；
- 飞书回调与健康页**如实**报告 replay 适配器状态。
"""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from ai_banking_copilot.policy.audit import KIND_CHANNEL, AuditLog
from ai_banking_copilot.web.app import AppDeps, create_app
from ai_banking_copilot.web.channels.identity import init_channel_schema
from ai_banking_copilot.web.session_repository import SQLiteSessionRepository, init_session_schema


def test_im_and_web_share_the_same_state_and_level(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))

    im = client.post("/api/im/message", json={"text": "转给李娜 500 元", "chat_id": "c1"})
    assert im.status_code == 200
    im_body = im.json()

    web = client.post("/api/submit", json={"utterance": "转给李娜 500 元"})
    assert web.status_code == 200
    web_body = web.json()

    assert im_body["state"] == web_body["state"] == "awaiting_confirm"
    assert im_body["level"] == web_body["card"]["level"] == "YELLOW"
    assert im_body["card"]["frozen_hash"] == web_body["card"]["frozen_hash"]
    assert im_body["channel"] == "im"
    assert im_body["rendered"]["text"]


def test_unbound_external_identity_is_rejected(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    response = client.post(
        "/api/im/message", json={"text": "查一下余额", "user_id": "ou_unknown"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "identity_unbound"


def test_same_event_id_replays_without_reexecuting(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    body = {"text": "转给李娜 500 元", "chat_id": "c2", "event_id": "evt-1"}

    first = client.post("/api/im/message", json=body)
    assert first.status_code == 200
    assert first.json()["replayed"] is False

    second = client.post("/api/im/message", json=body)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["state"] == first.json()["state"]


def test_rate_limit_uses_external_identity_then_falls_back_to_internal_user(
    deps: AppDeps, rw_store
) -> None:
    init_channel_schema(rw_store)
    for external_id in ("ou-a", "ou-b"):
        rw_store.execute(
            "INSERT INTO channel_identities(channel, external_user_id, user_id, bound_at) "
            "VALUES ('im', ?, ?, 'now')",
            (external_id, deps.user_id),
        )
    client = TestClient(create_app(replace(deps, channel_store=rw_store)))

    for external_id in ("ou-a", "ou-b"):
        responses = [
            client.post(
                "/api/im/message",
                json={
                    "text": "查一下余额",
                    "user_id": external_id,
                    "event_id": f"{external_id}-{index}",
                },
            )
            for index in range(21)
        ]
        assert [response.status_code for response in responses] == [200] * 20 + [429]

    local = [
        client.post(
            "/api/im/message",
            json={"text": "查一下余额", "event_id": f"local-{index}"},
        )
        for index in range(21)
    ]
    assert [response.status_code for response in local] == [200] * 20 + [429]


def test_audit_carries_channel_and_only_a_digest(deps: AppDeps, audit: AuditLog) -> None:
    client = TestClient(create_app(deps))
    client.post("/api/im/message", json={"text": "转给李娜 500 元", "chat_id": "c3"})

    channel_events = [event for event in audit.events() if event.kind == KIND_CHANNEL]
    assert channel_events
    payload = channel_events[-1].payload
    assert payload["channel"] == "im"
    assert payload["session_id"] == "im:c3"
    assert payload["text_length"] == len("转给李娜 500 元")
    assert payload["text_digest"]
    assert "转给李娜 500 元" not in str(payload)


def test_im_confirm_executes_through_the_shared_session(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    submitted = client.post(
        "/api/im/message", json={"text": "转给李娜 500 元", "chat_id": "c4"}
    ).json()
    card = submitted["card"]

    confirmed = client.post(
        "/api/im/confirm",
        json={
            "card_id": card["card_id"],
            "frozen_hash": card["frozen_hash"],
            "chat_id": "c4",
            "event_id": "evt-confirm-1",
        },
    )
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["state"] == "completed"
    assert body["kind"] == "receipt"
    assert body["record_ids"]


def test_plan_card_confirmation_survives_app_restart(deps: AppDeps, rw_store) -> None:
    init_session_schema(rw_store)
    persistent = replace(deps, session_repository=SQLiteSessionRepository(rw_store))
    first_client = TestClient(create_app(persistent))
    submitted = first_client.post(
        "/api/submit", json={"utterance": "转给李娜 500 元", "session_id": "restart"}
    ).json()
    first_client.close()

    second_client = TestClient(create_app(persistent))
    confirmed = second_client.post(
        "/api/confirm",
        json={
            "session_id": "restart",
            "card_id": submitted["card"]["card_id"],
            "frozen_hash": submitted["card"]["frozen_hash"],
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == "completed"
    assert confirmed.json()["record_ids"]
    second_client.close()


def test_feishu_callback_rejects_unsigned_request(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    response = client.post("/api/im/feishu", json={})
    assert response.status_code == 401
    assert response.json()["detail"] == "signature_invalid"


def test_health_reports_feishu_as_not_configured(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    body = client.get("/api/channels/health").json()
    assert body["channels"]["im"]["configured"] is True
    assert body["channels"]["feishu"]["configured"] is False
    assert body["channels"]["feishu"]["mode"] == "replay"
    assert body["channels"]["feishu"]["reason"] == "replay_mode"
    assert body["rate_limit"]["limit"] == 20
    expected = {
        "status": "unknown",
        "last_event_at": None,
        "events": 0,
        "ok": 0,
        "failed": 0,
        "replayed": 0,
        "retries": 0,
        "ratelimit_hits": 0,
    }
    for channel in ("web", "im", "feishu"):
        assert body["health"][channel] == {
            **expected,
            "replay_mode": channel == "feishu",
        }
