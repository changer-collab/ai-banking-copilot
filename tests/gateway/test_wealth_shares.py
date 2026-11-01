"""理财份额：申购写份额、按份额赎回、金额与份额同步。"""

from __future__ import annotations

from ai_banking_copilot.gateway.core_bank import CoreBank, init_wealth_holdings_schema
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store


def _bank(rw_store: Store) -> CoreBank:
    init_wealth_holdings_schema(rw_store)
    return CoreBank(rw_store)


def _holding(rw_store: Store, product_id: str) -> dict[str, int]:
    row = rw_store.query(
        "SELECT amount_cents, share_bp FROM wealth_holdings"
        " WHERE product_id = ? AND user_id = ?",
        (product_id, DEMO_USER_ID),
    )[0]
    return {"amount_cents": int(row["amount_cents"]), "share_bp": int(row["share_bp"])}


def test_subscribe_writes_shares(rw_store: Store) -> None:
    bank = _bank(rw_store)
    before = _holding(rw_store, "wp_bond")
    bank.subscribe_product(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        product_id="wp_bond",
        amount=Money(100000),
        idempotency_key="sub-shares-1",
    )
    after = _holding(rw_store, "wp_bond")
    assert after["amount_cents"] == before["amount_cents"] + 100000
    assert after["share_bp"] > before["share_bp"]


def test_redeem_by_shares_decreases_both_amount_and_shares(rw_store: Store) -> None:
    bank = _bank(rw_store)
    before = _holding(rw_store, "wp_bond")
    shares = before["share_bp"] // 2
    receipt = bank.redeem_product(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        product_id="wp_bond",
        amount=Money(0),
        shares=shares,
        idempotency_key="red-shares-1",
    )
    after = _holding(rw_store, "wp_bond")
    assert after["share_bp"] == before["share_bp"] - shares
    assert after["amount_cents"] < before["amount_cents"]
    assert receipt.shares_before == before["share_bp"]
    assert receipt.shares_after == after["share_bp"]


def test_redeem_more_than_holding_is_rejected(rw_store: Store) -> None:
    bank = _bank(rw_store)
    before = _holding(rw_store, "wp_bond")
    try:
        bank.redeem_product(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            product_id="wp_bond",
            amount=Money(0),
            shares=before["share_bp"] + 1,
            idempotency_key="red-shares-too-many",
        )
    except Exception as error:  # noqa: BLE001 - 只断言拒绝，不绑定具体异常类型
        assert "不足以赎回" in str(error) or "超过" in str(error)
    else:  # pragma: no cover - 失败时给出明确信号
        raise AssertionError("超额赎回必须被拒绝")
