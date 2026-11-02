"""订阅详情与取消状态的渠道层验收（`/api/options` + 取消结果）。"""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from ai_banking_copilot.gateway.store import Store
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
        json={"utterance": "关闭扣款协议", "selected": {"subscription": subscription}},
    ).json()
    card = submitted["card"]
    assert card is not None, submitted
    prompt = client.post("/api/verify/request", json={}).json()
    confirmed = client.post(
        "/api/confirm",
        json={
            "card_id": card["card_id"],
            "frozen_hash": card["frozen_hash"],
            "verification_code": prompt["code"],
        },
    ).json()
    return confirmed


def test_options_expose_subscription_details_from_raw_transactions(deps: AppDeps) -> None:
    response = _client(deps).get("/api/options")
    assert response.status_code == 200
    details = response.json()["subscription_details"]
    assert details["as_of"] == "2026-10-31"
    assert details["data_source_is_demo"] is True
    assert details["external_cancel_status"] == "not_integrated"

    by_id = {item["insight_id"]: item for item in details["insights"]}
    video = by_id["subscription:sub_video"]
    assert video["status"] == "confirmed"
    assert video["amount_cents"] == 2500
    assert video["currency"] == "CNY"
    assert video["cycle"] == "monthly"
    assert video["last_charged_on"] == "2026-10-18"
    assert video["next_charge_is_prediction"] is True
    assert "预测" in video["next_charge_basis"]
    assert video["evidence"]

    for item in video["evidence"]:
        record = deps.gateway.transaction(item["txn_id"])
        assert item["record_id"] == record.record_id
        assert item["content_hash"] == record.content_hash
        assert item["amount_cents"] == record.payload.amount.cents
        assert item["booked_on"] == record.payload.booked_on.isoformat()
        assert item["booked_at"] == record.payload.booked_at
        assert item["card_tail"] == record.payload.card_tail
        assert item["channel"] == record.payload.channel

    assert video["price_increased_12m"] is False
    assert video["price_change_12m_from_cents"] == 2500
    assert video["price_change_12m_to_cents"] == 2500
    latest = video["evidence"][-1]
    assert latest["booked_at"] == "2026-10-18T20:30:00"
    assert latest["card_tail"] == "4821"
    assert latest["channel"] == "app"
    music = by_id["subscription:sub_music"]
    assert music["price_increased_12m"] is True
    assert music["price_change_12m_from_cents"] == 1000
    assert music["price_change_12m_to_cents"] == 1200


def test_options_keep_missing_charge_time_and_card_tail_null(
    deps: AppDeps, rw_store: Store,
) -> None:
    with rw_store.transaction():
        rw_store.execute(
            "UPDATE transactions SET booked_at = NULL, card_tail = NULL "
            "WHERE merchant = ?",
            ("视频会员",),
        )
    details = _client(deps).get("/api/options").json()["subscription_details"]
    video = next(
        item for item in details["insights"] if item["insight_id"] == "subscription:sub_video"
    )
    assert video["evidence"]
    for item in video["evidence"]:
        assert item["booked_at"] is None
        assert item["card_tail"] is None


def test_options_disclose_subscription_cancel_capabilities(deps: AppDeps) -> None:
    capabilities = _client(deps).get("/api/options").json()["subscription_capabilities"]
    assert capabilities["external_cancel_status"] == "not_integrated"
    assert capabilities["external_cancel_integrated"] is False
    assert capabilities["can_request_external_cancel"] is False
    assert capabilities["can_local_cancel"] is True
    assert "尚未接入" in capabilities["boundary"]
    assert {item["state"] for item in capabilities["states"]} == CANCEL_STATES


def test_options_expose_false_price_state_without_changing_evidence_contract(
    deps: AppDeps, rw_store: Store,
) -> None:
    with rw_store.transaction():
        rw_store.execute(
            "UPDATE transactions SET amount_cents = -2500 WHERE merchant = ?",
            ("视频会员",),
        )
    details = _client(deps).get("/api/options").json()["subscription_details"]
    video = next(item for item in details["insights"]
                 if item["insight_id"] == "subscription:sub_video")
    assert video["price_increased_12m"] is False
    assert video["price_change_12m_from_cents"] == 2500
    assert video["price_change_12m_to_cents"] == 2500
    assert video["next_charge_on"]
    for evidence in video["evidence"]:
        assert {"merchant_label", "amount_cents", "booked_at", "booked_on", "card_tail",
                "channel", "record_id", "content_hash"} <= evidence.keys()


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
        json={"utterance": "关闭扣款协议", "selected": {"subscription": "sub_video"}},
    ).json()
    assert submitted["state"] == "blocked"
    assert submitted["card"] is None
    assert "安全拒绝" in submitted["message"]
