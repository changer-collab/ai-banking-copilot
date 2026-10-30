"""多轮指代：只补上一笔已执行的事实，不猜、不新增业务动作。"""

from fastapi.testclient import TestClient

from ai_banking_copilot.planning.anaphora import (
    DialogueMemory,
    ReferenceKind,
    resolve_reference,
)
from ai_banking_copilot.web.app import AppDeps, create_app


def _confirm(client: TestClient, view: dict) -> dict:
    card = view["card"]
    body = {"card_id": card["card_id"], "frozen_hash": card["frozen_hash"]}
    if card["level"] == "RED":
        prompt = client.post("/api/verify/request", json={}).json()
        body["verification_code"] = prompt["code"]
    response = client.post("/api/confirm", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _first_transfer(deps: AppDeps, amount: str = "500 元") -> TestClient:
    client = TestClient(create_app(deps))
    first = client.post("/api/submit", json={"utterance": f"转给李娜 {amount}"}).json()
    assert first["state"] == "awaiting_confirm", first.get("message")
    assert _confirm(client, first)["state"] == "completed"
    return client


def test_amount_only_reuses_last_payee(deps: AppDeps) -> None:
    client = _first_transfer(deps)
    again = client.post("/api/submit", json={"utterance": "再转 200"}).json()

    assert again["state"] == "awaiting_confirm", again.get("message")
    lines = "".join(again["card"]["lines"])
    assert "李娜" in lines and "200.00" in lines
    assert "已按上一笔补全" in again["message"]


def test_repeat_uses_the_same_amount(deps: AppDeps) -> None:
    client = _first_transfer(deps, "300 元")
    again = client.post("/api/submit", json={"utterance": "再转一次"}).json()

    assert again["state"] == "awaiting_confirm", again.get("message")
    assert "300.00" in "".join(again["card"]["lines"])


def test_switch_account_is_visible_in_the_note(deps: AppDeps) -> None:
    client = _first_transfer(deps)
    switched = client.post("/api/submit", json={"utterance": "换个账户转 100"}).json()

    assert switched["state"] == "awaiting_confirm", switched.get("message")
    assert "改换账户" in switched["message"]


def test_undo_last_reverses_the_previous_execution(deps: AppDeps) -> None:
    client = _first_transfer(deps)
    undo = client.post("/api/submit", json={"utterance": "刚才那笔取消"}).json()

    assert undo["state"] == "reversed", undo
    assert client.get("/api/session").json()["state"] == "reversed"


def test_reference_without_memory_does_not_invent(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    view = client.post("/api/submit", json={"utterance": "再转 200"}).json()
    assert view["state"] != "awaiting_confirm"


def test_resolver_is_pure_and_guards_unknown_memory() -> None:
    memory = DialogueMemory(action="transfer.create", payee_id="p_lina", amount_cents=50000)
    assert resolve_reference("再转 200", memory).kind is ReferenceKind.AMOUNT_ONLY
    assert resolve_reference("再转 200", memory).amount_cents == 20000
    assert resolve_reference("再转一次", memory).kind is ReferenceKind.REPEAT
    assert resolve_reference("刚才那笔取消", memory).kind is ReferenceKind.UNDO
    assert resolve_reference("换个账户转 100", memory).kind is ReferenceKind.SWITCH_ACCOUNT
    assert resolve_reference("再转 200", None).kind is ReferenceKind.NONE
    card_memory = DialogueMemory(action="card.lock")
    assert resolve_reference("再转 200", card_memory).kind is ReferenceKind.NONE
