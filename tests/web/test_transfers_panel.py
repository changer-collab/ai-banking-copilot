"""转账记录面板（前端③）：AI 做过的事必须查得到、点得开，且**只读**。

断言三件事：
1. 页面确实有「转账记录」面板；
2. 执行一笔转账后，`GET /api/transfers` 能查到它，收款人**已脱敏**、金额与记账日与事实一致；
3. 该端点只读：参数越界直接 400，不产生任何写操作。
"""

from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app


def test_page_contains_the_transfer_records_panel(deps: AppDeps) -> None:
    html = TestClient(create_app(deps)).get("/").text
    assert 'id="transfers-panel"' in html
    assert 'id="transfers-list"' in html


def test_executed_transfer_is_listed_with_masked_payee(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    submitted = client.post("/api/submit", json={"utterance": "转给李娜 500 元"}).json()
    card = submitted["card"]
    confirmed = client.post(
        "/api/confirm",
        json={"card_id": card["card_id"], "frozen_hash": card["frozen_hash"]},
    )
    assert confirmed.status_code == 200

    payload = client.get("/api/transfers").json()
    assert payload["window_days"] == 30
    assert payload["as_of"] == deps.today.isoformat()
    matches = [
        item
        for item in payload["items"]
        if item["kind"] == "transaction" and item["amount_cents"] == 50000
    ]
    assert matches, "刚执行完的转账必须能在记录里查到"
    record = matches[-1]
    assert record["payee"] == "李*", "收款人姓名必须脱敏（李娜 → 李*）"
    assert record["status"] == "已到账"
    assert record["record_id"].startswith("txn")
    assert record["account"] == "储蓄账户"


def test_transfer_records_endpoint_is_read_only(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    assert client.get("/api/transfers", params={"days": 0}).status_code == 400
    assert client.get("/api/transfers", params={"days": 400}).status_code == 400
    before = len(client.get("/api/transfers").json()["items"])
    client.get("/api/transfers", params={"days": 1})
    assert len(client.get("/api/transfers").json()["items"]) == before


def test_payee_mask_keeps_first_and_last_character() -> None:
    from ai_banking_copilot.web.app import _mask_payee_name

    assert _mask_payee_name("李娜") == "李*"
    assert _mask_payee_name("欧阳娜娜") == "欧*娜"
    assert _mask_payee_name("") == "收款人已隐藏"
    assert _mask_payee_name("陈") == "收款人已隐藏"
