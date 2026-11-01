"""赎回试算：全部 / 份额 / 金额、到账、快赎、大额与封闭期。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app


def _client(deps: AppDeps) -> TestClient:
    return TestClient(create_app(deps))


def test_matured_bond_full_redemption_quote(deps: AppDeps) -> None:
    data = _client(deps).get(
        "/api/wealth/redemption-quote", params={"product_id": "wp_bond", "mode": "all"}
    ).json()
    assert data["redeemable"] is True
    assert data["amount_yuan"] == "3000.00"
    assert data["arrival"]["mode"] == "t_plus_1"
    assert data["large_redemption"] is True
    assert data["percent_of_holding"] == 100.0


def test_share_redemption_quote_converts_amount(deps: AppDeps) -> None:
    data = _client(deps).get(
        "/api/wealth/redemption-quote",
        params={"product_id": "wp_bond", "mode": "shares", "value": "1000.0000"},
    ).json()
    assert data["redeemable"] is True
    assert data["shares"] == "1000.0000"
    assert data["amount_yuan"] == "1007.50"


def test_closed_bond_is_not_redeemable(deps: AppDeps) -> None:
    data = _client(deps).get(
        "/api/wealth/redemption-quote", params={"product_id": "wp_short", "mode": "all"}
    ).json()
    assert data["redeemable"] is False
    assert "封闭期" in data["reason"]


def test_cash_fast_redemption_is_realtime(deps: AppDeps) -> None:
    data = _client(deps).get(
        "/api/wealth/redemption-quote", params={"product_id": "wp_cash", "mode": "all"}
    ).json()
    assert data["redeemable"] is True
    assert data["fast_redeem"] is True
    assert data["arrival"]["mode"] == "realtime"


def test_invalid_mode_is_rejected(deps: AppDeps) -> None:
    response = _client(deps).get(
        "/api/wealth/redemption-quote", params={"product_id": "wp_bond", "mode": "xxx"}
    )
    assert response.status_code == 400
