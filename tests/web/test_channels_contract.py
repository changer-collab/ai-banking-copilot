"""P0-4 渠道内核契约：IM 与 Web 走**同一套**会话状态机，且身份 / 幂等 / 审计纪律成立。

覆盖：
- 同一句话在 IM 与 Web 得到**同一状态与同一等级**（渠道只搬消息，不二次裁决）；
- 外部身份未绑定 ⇒ 401 `identity_unbound`（fail-closed）；
- 同一 `event_id` 第二次直接回放（`replayed=True`）；
- 审计带 `channel` 且只落摘要；
- `/api/im/confirm` 能真正完成执行；
- 飞书回调与健康页**如实**报告「未接入」。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ai_banking_copilot.policy.audit import KIND_CHANNEL, AuditLog
from ai_banking_copilot.web.app import AppDeps, create_app


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


def test_feishu_callback_is_honestly_not_configured(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    response = client.post("/api/im/feishu", json={})
    assert response.status_code == 501
    assert response.json()["detail"] == "not_configured"


def test_health_reports_feishu_as_not_configured(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    body = client.get("/api/channels/health").json()
    assert body["channels"]["im"]["configured"] is True
    assert body["channels"]["feishu"]["configured"] is False
    assert body["channels"]["feishu"]["reason"] == "not_configured"
    assert body["rate_limit"]["limit"] == 20
