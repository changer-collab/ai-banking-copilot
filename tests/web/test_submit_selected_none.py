"""账户槽口径：页面把未选的下拉框以 `null` 提交，不能因此比「不传」多问一轮。

验收时发现同一句话在页面上要先选账户、直接调接口却直接出卡（2026-10-27 验收记录 §5 缺陷 8）。
口径统一为：**显式 null = 未指定**，与不传等价；显式给了账户 id 仍然优先。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.web.app import AppDeps, create_app


def _submit(deps: AppDeps, selected: dict[str, object] | None) -> dict[str, object]:
    client = TestClient(create_app(deps))
    body: dict[str, object] = {
        "session_id": f"slot-{uuid.uuid4().hex[:8]}",
        "utterance": "转给李娜 500 元",
    }
    if selected is not None:
        body["selected"] = selected
    response = client.post("/api/submit", json=body)
    assert response.status_code == 200
    payload: dict[str, object] = response.json()
    return payload


def test_null_account_is_treated_as_unspecified(deps: AppDeps) -> None:
    payload = _submit(deps, {"account": None})
    assert payload["state"] == "awaiting_confirm"
    assert payload["clarifications"] == []
    assert payload["card"] is not None


def test_absent_and_null_selected_are_equivalent(deps: AppDeps) -> None:
    absent = _submit(deps, None)
    nulled = _submit(deps, {"account": None, "payee": None, "amount": None})
    assert absent["state"] == nulled["state"] == "awaiting_confirm"
    assert absent["card"]["level"] == nulled["card"]["level"]  # type: ignore[index]


def test_explicit_account_still_wins(deps: AppDeps) -> None:
    """显式给了账户 id 仍然优先：钱从所选账户扣，不落回默认活期储蓄。"""
    before = {
        record.payload.account_id: record.payload.balance.cents
        for record in deps.gateway.accounts_of(DEMO_USER_ID)
    }
    client = TestClient(create_app(deps))
    session_id = f"slot-{uuid.uuid4().hex[:8]}"
    submitted = client.post(
        "/api/submit",
        json={
            "session_id": session_id,
            "utterance": "转给李娜 500 元",
            "selected": {"account": "acc_current"},
        },
    ).json()
    assert submitted["state"] == "awaiting_confirm"
    card = submitted["card"]
    confirmed = client.post(
        "/api/confirm",
        json={
            "session_id": session_id,
            "card_id": card["card_id"],
            "frozen_hash": card["frozen_hash"],
        },
    ).json()
    assert confirmed["state"] == "completed"
    after = {
        record.payload.account_id: record.payload.balance.cents
        for record in deps.gateway.accounts_of(DEMO_USER_ID)
    }
    assert before["acc_current"] - after["acc_current"] == 50000
    assert before["acc_savings"] == after["acc_savings"]
