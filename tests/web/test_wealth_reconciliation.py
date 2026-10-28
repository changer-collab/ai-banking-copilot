"""持仓对账（H1/H2）：申购、赎回后「余额变动 == 持仓变动」，红色动作未验证不动钱。

对账口径：`accounts.balance_cents` 与 `wealth_holdings.amount_cents` 必须同源同增同减，
且每笔资金流都能在 `transactions` 里按产品查到（操作可查）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.web.app import AppDeps, create_app

_SESSION = "reconcile"
_ACCOUNT = "acc_savings"


def _client(deps: AppDeps) -> TestClient:
    return TestClient(create_app(deps))


def _balance(store: Store, account_id: str = _ACCOUNT) -> int:
    rows = store.query(
        "SELECT balance_cents FROM accounts WHERE account_id = ?", (account_id,)
    )
    return int(rows[0]["balance_cents"])


def _holding(store: Store, product_id: str) -> int:
    rows = store.query(
        "SELECT amount_cents FROM wealth_holdings WHERE product_id = ? AND user_id = ?",
        (product_id, DEMO_USER_ID),
    )
    return int(rows[0]["amount_cents"]) if rows else 0


def _submit(
    client: TestClient, utterance: str, selected: dict[str, object]
) -> dict[str, object]:
    response = client.post(
        "/api/submit",
        json={"utterance": utterance, "selected": selected, "session_id": _SESSION},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _transactions_of(store: Store, product_id: str) -> list[object]:
    return store.query(
        "SELECT amount_cents, direction FROM transactions"
        " WHERE merchant = ? ORDER BY rowid",
        (product_id,),
    )


def _as_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def test_subscribe_reconciles_balance_and_holding(
    deps: AppDeps, rw_store: Store
) -> None:
    client = _client(deps)
    amount_cents = 50000
    before_balance = _balance(rw_store)
    before_holding = _holding(rw_store, "wp_cash")

    submitted = _submit(
        client, "申购 500 元理财", {"account": _ACCOUNT, "product": "wp_cash"}
    )
    assert submitted["state"] == "awaiting_confirm"
    card = submitted["card"]
    assert isinstance(card, dict)
    assert card["level"] == "RED"
    assert "strong_verify" in card["requires"]

    # 红色动作：未通过强验证，一分钱都不能动
    denied = client.post(
        "/api/confirm",
        json={
            "session_id": _SESSION,
            "card_id": card["card_id"],
            "frozen_hash": card["frozen_hash"],
        },
    )
    assert denied.status_code == 409
    assert _balance(rw_store) == before_balance
    assert _holding(rw_store, "wp_cash") == before_holding

    prompt = client.post("/api/verify/request", json={"session_id": _SESSION}).json()
    assert prompt["code"]
    done = client.post(
        "/api/confirm",
        json={
            "session_id": _SESSION,
            "card_id": card["card_id"],
            "frozen_hash": card["frozen_hash"],
            "verification_code": prompt["code"],
        },
    ).json()
    assert done["state"] == "completed", done

    after_balance = _balance(rw_store)
    after_holding = _holding(rw_store, "wp_cash")
    balance_delta = after_balance - before_balance
    holding_delta = after_holding - before_holding
    assert holding_delta == amount_cents
    assert balance_delta == -amount_cents
    assert balance_delta == -holding_delta

    # 操作可查：回执可对账，交易流水按产品可查
    record_ids = _as_list(done["record_ids"])
    assert record_ids
    assert any(str(item).startswith("transactions:") for item in record_ids)
    rows = _transactions_of(rw_store, "wp_cash")
    assert rows
    assert int(rows[-1]["amount_cents"]) == -amount_cents
    assert rows[-1]["direction"] == "out"
    assert deps.audit.verify_chain()


def test_redeem_reconciles_balance_and_holding_and_ignores_min_amount(
    deps: AppDeps, rw_store: Store
) -> None:
    """赎回 500 元低于 wp_bond 的 1000 元起购：必须成功（H2），且余额与持仓反向等额变动。"""
    client = _client(deps)
    amount_cents = 50000
    before_balance = _balance(rw_store)
    before_holding = _holding(rw_store, "wp_bond")

    submitted = _submit(
        client, "赎回 500 元理财", {"account": _ACCOUNT, "product": "wp_bond"}
    )
    assert submitted["state"] == "awaiting_confirm"
    card = submitted["card"]
    assert isinstance(card, dict)
    done = client.post(
        "/api/confirm",
        json={
            "session_id": _SESSION,
            "card_id": card["card_id"],
            "frozen_hash": card["frozen_hash"],
        },
    ).json()
    assert done["state"] == "completed", done

    after_balance = _balance(rw_store)
    after_holding = _holding(rw_store, "wp_bond")
    assert before_holding - after_holding == amount_cents
    assert after_balance - before_balance == amount_cents
    assert after_balance - before_balance == before_holding - after_holding

    rows = _transactions_of(rw_store, "wp_bond")
    assert rows
    assert int(rows[-1]["amount_cents"]) == amount_cents
    assert rows[-1]["direction"] == "in"
    assert deps.audit.verify_chain()


def test_redeem_beyond_holding_is_refused_and_writes_nothing(
    deps: AppDeps, rw_store: Store
) -> None:
    client = _client(deps)
    before_balance = _balance(rw_store)
    before_holding = _holding(rw_store, "wp_bond")
    before_transactions = len(_transactions_of(rw_store, "wp_bond"))

    submitted = _submit(
        client, "赎回 99999 元理财", {"account": _ACCOUNT, "product": "wp_bond"}
    )
    if submitted["state"] == "blocked":
        assert _balance(rw_store) == before_balance
        assert _holding(rw_store, "wp_bond") == before_holding
        return
    card = submitted["card"]
    assert isinstance(card, dict)
    done = client.post(
        "/api/confirm",
        json={
            "session_id": _SESSION,
            "card_id": card["card_id"],
            "frozen_hash": card["frozen_hash"],
        },
    )
    # 赎回属于黄档（非红线），无需强验证；执行期余额校验失败如实报 FAILED/PARTIAL，绝不谎报成功。
    assert done.status_code == 200, done.text
    assert done.json()["state"] in {"failed", "partial"}, done.text
    assert _balance(rw_store) == before_balance
    assert _holding(rw_store, "wp_bond") == before_holding
    assert len(_transactions_of(rw_store, "wp_bond")) == before_transactions
