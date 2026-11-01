"""理财面板聚合接口：产品逐只判定 + 原因、持仓事实、未测评不默认等级、只读。"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.risk_store import save_risk_assessment
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.scenarios.wealth import (
    HOLDING_INCOME_UNKNOWN,
    PRODUCT_DATA_SOURCE,
    PRODUCT_NOT_ASSESSED,
)
from ai_banking_copilot.web.app import AppDeps, create_app


def _client(deps: AppDeps) -> TestClient:
    return TestClient(create_app(deps))


def _overview(deps: AppDeps) -> dict[str, object]:
    response = _client(deps).get("/api/wealth/overview")
    assert response.status_code == 200
    return response.json()


def _by_product(payload: dict[str, object], key: str) -> dict[str, dict[str, object]]:
    rows = payload[key]
    assert isinstance(rows, list)
    return {str(row["product_id"]): row for row in rows}


def _business_state(store: Store) -> tuple[int, int, int]:
    transactions = store.query("SELECT COUNT(*) AS n FROM transactions")[0]["n"]
    receipts = store.query("SELECT COUNT(*) AS n FROM write_receipts")[0]["n"]
    balances = store.query("SELECT COALESCE(SUM(balance_cents), 0) AS n FROM accounts")[0]["n"]
    return int(transactions), int(receipts), int(balances)


def test_r2_catalog_greys_out_products_with_written_reason(
    deps: AppDeps, rw_store: Store
) -> None:
    save_risk_assessment(rw_store, DEMO_USER_ID, "R2", DEFAULT_BOOKING_DATE)
    payload = _overview(deps)
    products = _by_product(payload, "products")

    for product_id in ("wp_cash", "wp_short", "wp_bond"):
        assert products[product_id]["eligible"] is True
        assert products[product_id]["reason"] == ""
    for product_id in ("wp_mix", "wp_equity"):
        assert products[product_id]["eligible"] is False
        assert "高于你的测评等级 R2" in str(products[product_id]["reason"])

    assessment = payload["assessment"]
    assert isinstance(assessment, dict)
    assert assessment["risk_profile"] == "R2"
    assert payload["data_source"] == PRODUCT_DATA_SOURCE


def test_catalog_keeps_unassessed_cards_out_and_marks_reason(deps: AppDeps) -> None:
    payload = _overview(deps)
    # 默认 seed 是「演示 R3」，来源必须如实标注，不能当成真实测评
    assessment = payload["assessment"]
    assert isinstance(assessment, dict)
    assert assessment["source"] == "seed"
    for row in _by_product(payload, "products").values():
        assert row["data_source"] == PRODUCT_DATA_SOURCE
        # 大类 + 指标 + 模拟净值曲线都随产品目录下发（曲线必须标 simulated）
        assert row["category"] in {"货币类", "债券类", "混合类", "权益类"}
        assert isinstance(row["yield_bp"], int)
        assert row["simulated"] is True
        assert isinstance(row["nav_series"], list) and len(row["nav_series"]) == 90
    assert _by_product(payload, "products")["wp_mix"]["eligible"] is True
    assert _by_product(payload, "products")["wp_mix"]["category"] == "混合类"


def test_expired_assessment_never_defaults_to_a_level(
    deps: AppDeps, rw_store: Store
) -> None:
    save_risk_assessment(rw_store, DEMO_USER_ID, "R4", date(2024, 1, 1), valid_days=30)
    payload = _overview(deps)
    assessment = payload["assessment"]
    assert isinstance(assessment, dict)
    assert assessment["state"] == "expired"
    for row in _by_product(payload, "products").values():
        assert row["eligible"] is False
        assert row["reason"] == PRODUCT_NOT_ASSESSED


def test_holdings_expose_real_facts_and_reference_income_from_simulated_nav(
    deps: AppDeps,
) -> None:
    payload = _overview(deps)
    holdings = _by_product(payload, "holdings")
    assert set(holdings) == {"wp_cash", "wp_short", "wp_bond"}

    assert holdings["wp_cash"]["name"] == "活期理财篮子"
    assert holdings["wp_cash"]["category"] == "货币类"
    assert holdings["wp_cash"]["amount_yuan"] == "500.00"
    assert holdings["wp_cash"]["purchased_on"] == "2026-06-10"
    assert holdings["wp_short"]["name"] == "稳盈短期 A"
    assert holdings["wp_short"]["category"] == "债券类"
    assert holdings["wp_short"]["amount_yuan"] == "2000.00"
    assert holdings["wp_short"]["purchased_on"] == DEFAULT_BOOKING_DATE.isoformat()
    assert holdings["wp_bond"]["name"] == "稳利债券 A"
    assert holdings["wp_bond"]["category"] == "债券类"
    assert holdings["wp_bond"]["amount_yuan"] == "3000.00"
    assert holdings["wp_bond"]["purchased_on"] == "2026-03-18"
    for row in holdings.values():
        # 有模拟净值：收益是参考值（必须带「模拟净值」标注），不是真实行情
        assert row["simulated"] is True
        assert str(row["current_income"]).startswith("参考收益（模拟净值）")
        assert isinstance(row["nav_series"], list) and len(row["nav_series"]) == 90
        assert row["nav_series"][0]["on_date"] == "2026-08-03"
        assert str(row["nav_start"]).endswith(row["nav_series"][0]["nav"])
        assert row["nav_now"] == row["nav_series"][-1]["nav"]
        assert isinstance(row["income_cents"], int)
        assert row["income_cents"] > 0
        assert str(row["record_id"]).startswith("wealth_holdings:")


def test_holdings_without_nav_keep_income_unknown_and_null(deps: AppDeps, rw_store: Store) -> None:
    """旧库没有净值行：不得补 0、不得推算盈亏，收益与接口字段都如实标未知/null。"""
    with rw_store.transaction() as conn:
        conn.execute("DELETE FROM wealth_nav")
    holdings = _by_product(_overview(deps), "holdings")
    for row in holdings.values():
        assert row["nav_series"] == []
        assert row["nav_start"] is None
        assert row["nav_now"] is None
        assert row["income_cents"] is None
        assert row["current_income"] == HOLDING_INCOME_UNKNOWN


def test_overview_is_read_only(deps: AppDeps, rw_store: Store) -> None:
    before = _business_state(rw_store)
    _overview(deps)
    assert _business_state(rw_store) == before


def test_catalog_carries_expandable_explanation(deps: AppDeps) -> None:
    """§8.1b：产品卡可展开「推荐理由 / 为什么不能买」，解释随 overview 一起下发。"""
    products = _by_product(_overview(deps), "products")
    for product_id, row in products.items():
        why = row["explanation"]
        assert isinstance(why, dict), product_id
        assert why["title"] in {"推荐理由", "为什么不能买"}
        assert why["eligible"] is row["eligible"]
        assert isinstance(why["reasons"], list) and why["reasons"]
        path = why["path"]
        assert isinstance(path, list) and path[0].startswith("产品「")
        assert why["data_source"] == PRODUCT_DATA_SOURCE
