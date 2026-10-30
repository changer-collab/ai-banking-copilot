"""消费建议只在月账单执行后出现，且不改变账户或订阅。"""

from sqlite3 import OperationalError

from fastapi.testclient import TestClient
from pytest import MonkeyPatch, mark

from ai_banking_copilot.gateway.errors import GatewayError
from ai_banking_copilot.web.app import AppDeps, create_app


def _submit(client: TestClient, utterance: str) -> dict[str, object]:
    response = client.post("/api/submit", json={"utterance": utterance})
    assert response.status_code == 200
    return response.json()


def _confirm(client: TestClient, submitted: dict[str, object], **extra: object) -> dict:
    card = submitted["card"]
    assert isinstance(card, dict)
    response = client.post("/api/confirm", json={
        "card_id": card["card_id"], "frozen_hash": card["frozen_hash"], **extra,
    })
    assert response.status_code == 200
    return response.json()


def test_monthly_bill_returns_sourced_advice_without_business_writes(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    accounts = deps.gateway.accounts_of(deps.user_id)
    subscriptions = deps.gateway.subscriptions_of(deps.user_id)
    result = _confirm(client, _submit(client, "查一下上月账单"))

    assert result["state"] == "completed"
    advice = result["bill_advice"]
    assert 1 <= len(advice) <= 3
    share = next(item for item in advice if item["kind"] == "category-share")
    assert share["period"] == "2026-09"
    assert "8260.00" in share["evidence"]
    assert "51.24%" in share["evidence"]
    assert "净支出" in share["evidence"]
    assert any(item["kind"] == "subscription-review" for item in advice)
    assert all(item["source_refs"] for item in advice)
    assert all(source["record_id"] and source["content_hash"]
               for item in advice for source in item["source_refs"])
    assert deps.gateway.accounts_of(deps.user_id) == accounts
    assert deps.gateway.subscriptions_of(deps.user_id) == subscriptions
    assert deps.audit.verify_chain()


def test_non_bill_submission_clears_previous_advice(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    assert _confirm(client, _submit(client, "查一下上月账单"))["bill_advice"]
    balance = _confirm(client, _submit(client, "查余额"))
    assert balance["bill_advice"] == []


def test_advice_waits_for_completion_and_appears_after_resume(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    submitted = _submit(client, "查看账单，再查看分类趋势")
    assert "bill_advice" not in submitted
    partial = _confirm(client, submitted, steps=1)
    assert partial["state"] == "executing"
    assert partial["bill_advice"] == []
    assert client.post("/api/interrupt", json={}).status_code == 200
    resumed = client.post("/api/resume", json={})
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "completed"
    assert resumed.json()["bill_advice"]


@mark.parametrize("failure", [GatewayError, OperationalError, ValueError])
def test_advice_failure_keeps_successful_execution_visible(
    deps: AppDeps, monkeypatch: MonkeyPatch, failure: type[Exception],
) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise failure("建议来源暂不可用")

    monkeypatch.setattr("ai_banking_copilot.web.session.build_bill_advice", unavailable)
    client = TestClient(create_app(deps))
    result = _confirm(client, _submit(client, "查一下上月账单"))
    assert result["state"] == "completed"
    assert result["results"]
    assert result["bill_advice"] == []
    assert result["bill_advice_message"] == "消费建议暂不可用，请稍后重新分析账单。"
