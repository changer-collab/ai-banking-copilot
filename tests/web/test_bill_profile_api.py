"""账单接口的画像 / 趋势字段：数字由服务端算好，页面只渲染。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app


def test_bill_analysis_exposes_trend_profile_and_category_share(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    response = client.get("/api/bills/analysis")

    assert response.status_code == 200
    data = response.json()

    assert [point["period"] for point in data["trend"]][-1] == data["period"]
    assert len(data["trend"]) == 6
    assert sum(item["expense_cents"] for item in data["category_share"]) == data["summary"][
        "expense_total_cents"
    ]

    profile = data["profile"]
    assert profile["top_category"] == data["category_share"][0]["category"]
    assert profile["top_category_cents"] == data["category_share"][0]["expense_cents"]
    assert profile["subscription_monthly_cents"] > 0
    risky = [
        item
        for item in data["risk_findings"]
        if item["status"] in {"confirmed", "suspected"}
    ]
    assert profile["suspicious_count"] == len(risky)


def test_cards_endpoint_returns_status_and_limit_for_the_table(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    data = client.get("/api/cards").json()

    assert data["cards"], "演示用户应至少有一张卡"
    for card in data["cards"]:
        assert card["card_id"]
        assert card["status_label"]
        assert isinstance(card["daily_limit_cents"], int)
