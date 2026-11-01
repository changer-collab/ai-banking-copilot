"""`/api/cards` 的状态允许动作：按钮由服务端计算，前端不自行扩展。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.web.app import AppDeps, create_app


def _card(client: TestClient, card_id: str) -> dict[str, object]:
    data = client.get("/api/cards").json()
    return next(item for item in data["cards"] if item["card_id"] == card_id)


def test_active_debit_card_exposes_split_limits(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    card = _card(client, "card_debit")
    assert card["masked_no"].endswith("4471")
    assert "card_debit" not in str(card["masked_no"])
    assert card["brand_label"] == "银联"
    assert card["expiry"] == "09/29"
    assert card["account_class"] == "Ⅰ"
    assert card["limits"]["kind"] == "debit"
    assert card["limits"]["atm_limit_cents"] == 500_000
    assert card["allowed_actions"] == [
        "temp_lock", "adjust_debit_limit", "report_lost", "replace", "apply_virtual",
    ]


def test_active_credit_card_has_no_daily_limit(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    card = _card(client, "card_credit")
    assert card["limits"]["kind"] == "credit"
    assert card["limits"]["credit_limit_cents"] == 5_000_000
    assert card["limits"]["cycle_limit_cents"] == 800_000
    assert "adjust_credit_limit" in card["allowed_actions"]
    assert "set_cycle_limit" in card["allowed_actions"]
    assert "adjust_debit_limit" not in card["allowed_actions"]


def test_locked_card_only_offers_unlock_and_report(deps: AppDeps, core_bank: CoreBank) -> None:
    core_bank.set_card_status(
        card_id="card_debit", status="locked", temp_lock_until="2026-11-05",
        reason="cant_find", occurred_on="2026-10-31", idempotency_key="allowed-lock-1",
    )
    client = TestClient(create_app(deps))
    card = _card(client, "card_debit")
    assert card["status"] == "locked"
    assert card["temp_lock"]["until"] == "2026-11-05"
    assert card["temp_lock"]["reason_label"] == "找不到卡"
    assert card["allowed_actions"] == ["unlock", "report_lost", "view_lock_detail"]


def test_lost_card_only_offers_replace_and_detail(deps: AppDeps, core_bank: CoreBank) -> None:
    core_bank.set_card_status(
        card_id="card_debit", status="lost", reason="stolen",
        occurred_on="2026-10-31", idempotency_key="allowed-lost-1",
    )
    client = TestClient(create_app(deps))
    card = _card(client, "card_debit")
    assert card["status"] == "lost"
    assert card["lost"]["reason_label"] == "被盗"
    assert card["allowed_actions"] == ["replace", "view_lost_detail"]


def test_replaced_lost_card_hides_replacement_button(deps: AppDeps, core_bank: CoreBank) -> None:
    core_bank.replace_card(
        card_id="card_debit",
        address="上海市浦东新区演示路 1 号",
        mail_method="standard",
        with_report=True,
        occurred_on="2026-10-31",
        idempotency_key="allowed-replace-1",
    )
    client = TestClient(create_app(deps))
    card = _card(client, "card_debit")
    assert card["allowed_actions"] == ["view_lost_detail"]
    assert card["replacement"]["new_card_tail"]
