"""P2-7 多会话：`create_app` 按 `session_id` 隔离（两个会话互不串扰）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app


def _client(deps: AppDeps) -> TestClient:
    return TestClient(create_app(deps))


def test_two_sessions_do_not_interfere(deps: AppDeps) -> None:
    client = _client(deps)
    first = client.post(
        "/api/submit", json={"utterance": "转给李娜 500 元", "session_id": "sess-a"}
    ).json()
    second = client.post(
        "/api/submit", json={"utterance": "查一下上月账单", "session_id": "sess-b"}
    ).json()
    assert first["state"] == "awaiting_confirm"
    assert second["state"] == "awaiting_confirm"
    card_a = first["card"]
    card_b = second["card"]
    assert card_a["card_id"] != card_b["card_id"]
    # A 的计划卡不能借 B 的会话执行：B 的待确认计划是账单（卡号不匹配 → 409）
    mismatch = client.post(
        "/api/confirm",
        json={
            "card_id": card_a["card_id"],
            "frozen_hash": card_a["frozen_hash"],
            "session_id": "sess-b",
        },
    )
    assert mismatch.status_code == 409
    done = client.post(
        "/api/confirm",
        json={
            "card_id": card_a["card_id"],
            "frozen_hash": card_a["frozen_hash"],
            "session_id": "sess-a",
        },
    ).json()
    assert done["state"] == "completed"


def test_missing_session_id_uses_a_stable_default(deps: AppDeps) -> None:
    """不带 `session_id` 的请求共用**同一个**缺省会话（既有行为不变）。"""
    client = _client(deps)
    first = client.post("/api/submit", json={"utterance": "转给李娜 500 元"}).json()
    client.post("/api/submit", json={"utterance": "查一下上月账单"})
    # 第二次提交落在同一（缺省）会话：pending 已是账单卡，第一张卡不再匹配
    stale = client.post(
        "/api/confirm",
        json={
            "card_id": first["card"]["card_id"],
            "frozen_hash": first["card"]["frozen_hash"],
        },
    )
    assert stale.status_code == 409
