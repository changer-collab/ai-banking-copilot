"""理财申购前推荐流：缺产品时先按测评等级推荐，不先问金额；计划卡字段与来源一致。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ai_banking_copilot.scenarios.wealth import FORBIDDEN_HINTS
from ai_banking_copilot.web.app import AppDeps, create_app
from ai_banking_copilot.web.session import Session, SessionState


def test_subscribe_without_product_returns_recommendation_not_amount_ask(
    deps: AppDeps,
) -> None:
    """「我要申购理财产品」⇒ 推荐视图（R3），不再追问金额、不生成计划。"""
    view = Session(deps).submit("我要申购理财产品")
    assert view.state is SessionState.IDLE
    assert view.wealth_recommend == "R3"
    assert view.card is None
    assert view.clarifications == ()


def test_recommendation_payload_lists_only_eligible_products(deps: AppDeps) -> None:
    """推荐只包含 `eligible` 产品；推荐理由只写适配事实，不写收益承诺。"""
    client = TestClient(create_app(deps))
    data = client.post("/api/submit", json={"utterance": "我要申购理财产品"}).json()
    assert data["state"] == "idle"
    payload = data["wealth_recommend"]
    assert payload["risk_profile"] == "R3"
    products = payload["products"]
    assert products
    assert {item["product_id"] for item in products} == {
        "wp_cash", "wp_short", "wp_bond", "wp_mix"
    }
    assert all(item["eligible"] is True for item in products)
    assert all(item["recommend_reason"] for item in products)
    reasons = " ".join(str(item["recommend_reason"]) for item in products)
    assert not any(hint in reasons for hint in FORBIDDEN_HINTS)
    assert {item["value"] for item in payload["categories"]} == {"货币类", "债券类", "混合类"}


def test_plan_card_repeats_the_product_amount_and_account(deps: AppDeps) -> None:
    """弹窗提交的 utterance + selected 落到计划卡：产品 / 金额 / 账户三处一致。"""
    view = Session(deps).submit(
        "申购 2000 元 稳利债券 A", selected={"account": "acc_savings"}
    )
    assert view.card is not None
    line = "\n".join(view.card.lines)
    assert "稳利债券 A" in line
    assert "2000.00" in line
    assert "储蓄账户" in line
