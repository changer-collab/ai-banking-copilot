"""理财交易确认留痕：四件套、阅读时长、抄录与类型差异确认。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app

_DOCS = {
    "prospectus": "demo-v1",
    "risk_disclosure": "demo-v1",
    "investor_rights": "demo-v1",
    "sales_agreement": "demo-v1",
}
_DECLARATION = "本人已充分理解产品风险，自愿承担相关投资风险"


def _client(deps: AppDeps) -> TestClient:
    return TestClient(create_app(deps))


def _ack(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": "subscribe",
        "product_id": "wp_equity",
        "account_id": "acc_savings",
        "doc_versions": dict(_DOCS),
        "read_seconds": {
            "prospectus": 3,
            "risk_disclosure": 15,
            "investor_rights": 3,
            "sales_agreement": 3,
        },
        "declaration_text": _DECLARATION,
        "dividend_method": "reinvest",
        "experience_confirmed": True,
        "loss_ack": True,
    }
    payload.update(overrides)
    return payload


def test_product_detail_returns_four_demo_documents(deps: AppDeps) -> None:
    data = _client(deps).get("/api/wealth/products/wp_bond").json()
    assert set(data["documents"]) == set(_DOCS)
    assert all(doc["is_demo"] is True for doc in data["documents"].values())
    assert data["documents"]["risk_disclosure"]["body"]


def test_ack_rejects_short_reading(deps: AppDeps) -> None:
    response = _client(deps).post(
        "/api/wealth/acknowledgements",
        json=_ack(
            read_seconds={
                "prospectus": 1,
                "risk_disclosure": 15,
                "investor_rights": 1,
                "sales_agreement": 1,
            }
        ),
    )
    assert response.status_code == 400
    assert "阅读" in response.json()["detail"]


def test_ack_rejects_wrong_declaration(deps: AppDeps) -> None:
    response = _client(deps).post(
        "/api/wealth/acknowledgements",
        json=_ack(declaration_text="我大概看过了"),
    )
    assert response.status_code == 400
    assert "逐字抄录" in response.json()["detail"]


def test_ack_accepts_complete_first_purchase(deps: AppDeps) -> None:
    response = _client(deps).post(
        "/api/wealth/acknowledgements", json=_ack()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "recorded"
    assert body["first_purchase"] is True
    assert body["declaration_required"] is True
    assert body["ack_id"].startswith("ack_")
