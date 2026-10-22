"""订阅详情与取消状态的渠道层验收（`/api/options` + 取消结果）。"""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app

CANCEL_STATES = {
    "guidance",
    "third_party_page",
    "cancel_requested",
    "local_simulated_cancelled",
    "already_local_cancelled",
    "external_confirmed_cancelled",
}


def _client(deps: AppDeps) -> TestClient:
    return TestClient(create_app(deps))


def _cancel(deps: AppDeps, *, subscription: str = "sub_video") -> dict:
    client = _client(deps)
    submitted = client.post(
        "/api/submit",
        json={"utterance": "取消订阅", "selected": {"subscription": subscription}},
    ).json()
    card = submitted["card"]
    assert card is not None, submitted
    confirmed = client.post(
        "/api/confirm",
        json={"card_id": card["card_id"], "frozen_hash": card["frozen_hash"]},
    ).json()
    return confirmed


def test_options_expose_subscription_details_from_raw_transactions(deps: AppDeps) -> None:
    response = _client(deps).get("/api/options")
    assert response.status_code == 200
    details = response.json()["subscription_details"]
    assert details["as_of"] == "2026-10-01"
    assert details["data_source_is_demo"] is True
    assert details["external_cancel_status"] == "not_integrated"

    by_id = {item["insight_id"]: item for item in details["insights"]}
    video = by_id["subscription:sub_video"]
    assert video["status"] == "confirmed"
    assert video["amount_cents"] == 2500
    assert video["currency"] == "CNY"
    assert video["cycle"] == "monthly"
    assert video["last_charged_on"] == "2026-09-18"
    assert video["next_charge_is_prediction"] is True
    assert "预测" in video["next_charge_basis"]
    assert video["evidence"]

    for item in video["evidence"]:
        record = deps.gateway.transaction(item["txn_id"])
        assert item["record_id"] == record.record_id
        assert item["content_hash"] == record.content_hash
        assert item["amount_cents"] == record.payload.amount.cents
        assert item["booked_on"] == record.payload.booked_on.isoformat()


def test_options_disclose_subscription_cancel_capabilities(deps: AppDeps) -> None:
    capabilities = _client(deps).get("/api/options").json()["subscription_capabilities"]
    assert capabilities["external_cancel_status"] == "not_integrated"
    assert capabilities["external_cancel_integrated"] is False
    assert capabilities["can_request_external_cancel"] is False
    assert capabilities["can_local_cancel"] is True
    assert "尚未接入" in capabilities["boundary"]
    assert {item["state"] for item in capabilities["states"]} == CANCEL_STATES


def test_cancel_reports_local_simulation_not_merchant_confirmation(deps: AppDeps) -> None:
    confirmed = _cancel(deps)
    assert confirmed["state"] == "completed"
    cancel = confirmed["subscription_cancel"]
    assert cancel is not None
    assert cancel["state"] == "local_simulated_cancelled"
    assert cancel["label"] == "本地已取消（商户未确认）"
    assert cancel["external_status"] == "not_integrated"
    assert cancel["external_confirmed"] is False
    assert cancel["previous_status"] == "active"
    text = confirmed["message"] + cancel["message"] + cancel["boundary"]
    assert "商户已确认" not in text
    assert "商户已取消" not in text
    assert "取消成功" not in text


def test_repeated_cancel_has_no_new_side_effects(deps: AppDeps) -> None:
    first = _cancel(deps)
    assert first["subscription_cancel"]["state"] == "local_simulated_cancelled"
    assert first["subscription_cancel"]["effect_ids"] == ["subscription_cancel_sub_video"]

    second = _cancel(deps)
    cancel = second["subscription_cancel"]
    assert cancel["state"] == "already_local_cancelled"
    assert cancel["previous_status"] == "cancelled"
    assert cancel["effect_ids"] == []
    assert cancel["external_confirmed"] is False


def test_cancel_of_another_users_subscription_is_blocked(deps: AppDeps) -> None:
    client = _client(replace(deps, user_id="u_other"))
    submitted = client.post(
        "/api/submit",
        json={"utterance": "取消订阅", "selected": {"subscription": "sub_video"}},
    ).json()
    assert submitted["state"] == "blocked"
    assert submitted["card"] is None
    assert "安全拒绝" in submitted["message"]
