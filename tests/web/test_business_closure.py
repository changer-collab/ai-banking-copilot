"""通过真实 HTTP 路由核对业务闭环及重复检查的资金影响。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_banking_copilot.compose import build_deps
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.scenarios.cross import BIRTHDAY_ORDER_CENTS
from ai_banking_copilot.web.app import AppDeps, create_app


def _deps(tmp_path: Path) -> AppDeps:
    return build_deps(
        tmp_path / "closure.db",
        {"AIB_DISABLE_LLM": "1", "AIB_BREAKER_STATE": "memory", "AIB_AUDIT_KEY": "test"},
    )


def _confirm(client: TestClient, view: dict, session_id: str = "closure") -> dict:
    card = view["card"]
    body = {
        "session_id": session_id,
        "card_id": card["card_id"],
        "frozen_hash": card["frozen_hash"],
    }
    if card["level"] == "RED":
        prompt = client.post("/api/verify/request", json={"session_id": session_id}).json()
        body["verification_code"] = prompt["code"]
    response = client.post("/api/confirm", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_options_are_real_owned_records(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    client = TestClient(create_app(deps))
    response = client.get("/api/options")
    assert response.status_code == 200
    payload = response.json()
    assert payload["today"] == "2026-10-01"
    assert {item["id"] for item in payload["accounts"]} == {
        record.payload.account_id for record in deps.gateway.accounts_of(deps.user_id)
    }
    assert payload["holds"] == []


def test_subscription_string_answers_complete_and_persist(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    client = TestClient(create_app(deps))
    first = client.post(
        "/api/submit",
        json={"utterance": "代扣签约", "selected": {"account": "acc_savings"},
              "session_id": "closure"},
    ).json()
    assert first["state"] == "needs_clarification"
    assert "user_id" not in {ask["slot"] for ask in first["clarifications"]}
    ready = client.post(
        "/api/clarify",
        json={"session_id": "closure", "answers": {
            "merchant": "演示月刊", "amount_cents": "25元", "period": "每月",
            "next_charge_on": "2026-11-01",
        }},
    ).json()
    assert ready["state"] == "awaiting_confirm", ready
    result = _confirm(client, ready)
    assert result["state"] == "completed", result
    signed = [record.payload for record in deps.gateway.subscriptions_of(deps.user_id)
              if record.payload.merchant == "演示月刊"]
    assert len(signed) == 1
    assert signed[0].amount.cents == 2500
    assert signed[0].period == "monthly"
    assert signed[0].next_charge_on.isoformat() == "2026-11-01"


def test_due_requires_confirmation_and_is_not_repeated(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    client = TestClient(create_app(deps))
    initial = deps.gateway.account("acc_savings").payload.balance.cents
    booked = client.post("/api/submit", json={
        "utterance": "登记预约转账 给李娜 100元 2026-10-01",
        "selected": {"account": "acc_savings"}, "session_id": "closure",
    }).json()
    assert booked["state"] == "awaiting_confirm", booked
    assert _confirm(client, booked)["state"] == "completed"
    assert deps.gateway.account("acc_savings").payload.balance.cents == initial
    due = client.post("/api/due", json={"session_id": "due-1"}).json()
    assert due["state"] == "awaiting_confirm", due
    assert deps.gateway.account("acc_savings").payload.balance.cents == initial
    assert _confirm(client, due, "due-1")["state"] == "completed"
    # /api/due 同时装配到期预约转账与生日预算链（冻结→下单→解冻），后者从储蓄账户结算。
    expected = initial - 10000 - BIRTHDAY_ORDER_CENTS
    assert deps.gateway.account("acc_savings").payload.balance.cents == expected
    again = client.post("/api/due", json={"session_id": "due-2"}).json()
    assert again["state"] == "idle", again
    assert again["card"] is None
    assert deps.gateway.account("acc_savings").payload.balance.cents == expected
    assert deps.audit.verify_chain()


def test_client_cannot_advance_due_clock(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    client = TestClient(create_app(deps))
    booked = client.post("/api/submit", json={
        "utterance": "登记预约转账 给李娜 100元 2026-12-01",
        "selected": {"account": "acc_savings"}, "session_id": "closure",
    }).json()
    assert booked["state"] == "awaiting_confirm", booked
    assert _confirm(client, booked)["state"] == "completed"
    due = client.post("/api/due", json={
        "session_id": "due", "as_of": "2026-12-31",
    }).json()
    assert not any("转账" in line for line in due["card"]["lines"])


@pytest.mark.parametrize(
    ("utterance", "selected"),
    [
        ("查一下余额", {"account_id": "acc_other"}),
        ("查一下余额", {"account": "acc_savings", "account_id": "acc_other"}),
        ("解冻资金", {"hold_id": "hold_other"}),
    ],
)
def test_internal_object_aliases_cannot_access_another_user(
    tmp_path: Path, utterance: str, selected: dict
) -> None:
    deps = _deps(tmp_path)
    store = Store(tmp_path / "closure.db")
    with store.transaction() as conn:
        conn.execute("INSERT INTO users VALUES ('u_other', '其他用户', 'R1')")
        conn.execute("INSERT INTO accounts VALUES ('acc_other', 'u_other', 'savings', 5000, 'CNY')")
        conn.execute("INSERT INTO holds VALUES ('hold_other', 'acc_other', 5000, '测试', 'active')")
    client = TestClient(create_app(deps))
    view = client.post("/api/submit", json={
        "utterance": utterance, "selected": selected,
    }).json()
    assert view["state"] == "blocked", view
    assert view["card"] is None
    assert store.query("SELECT status FROM holds WHERE hold_id='hold_other'")[0][0] == "active"
    balance = store.query("SELECT balance_cents FROM accounts WHERE account_id='acc_other'")[0][0]
    assert balance == 5000
    store.close()
