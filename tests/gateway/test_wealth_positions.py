"""理财资金流：申购写持仓、赎回扣持仓（H1），赎回不受起购金额约束（H2）。"""

import pytest

from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.errors import (
    CoreBankError,
    InsufficientHoldingError,
)
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store


def _balance(store: Store, account_id: str) -> int:
    rows = store.query("SELECT balance_cents FROM accounts WHERE account_id = ?", (account_id,))
    return int(rows[0]["balance_cents"])


def _holding(store: Store, product_id: str) -> int:
    rows = store.query(
        "SELECT amount_cents FROM wealth_holdings WHERE product_id = ? AND user_id = ?",
        (product_id, DEMO_USER_ID),
    )
    return int(rows[0]["amount_cents"]) if rows else 0


def test_subscribe_increases_holding_and_returns_it(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    before_holding = _holding(rw_store, "wp_cash")
    before_balance = _balance(rw_store, "acc_savings")

    receipt = bank.subscribe_product(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        product_id="wp_cash",
        amount=Money(100000),
        idempotency_key="sub-1",
    )

    assert receipt.status == "settled"
    assert receipt.holding_before == before_holding
    assert receipt.holding_after == before_holding + 100000
    assert _holding(rw_store, "wp_cash") == before_holding + 100000
    assert _balance(rw_store, "acc_savings") == before_balance - 100000


def test_redeem_below_min_amount_but_within_holding_succeeds(rw_store: Store) -> None:
    """H2：赎回 500 元低于 wp_bond 的 1000 元起购，也必须成功。"""
    bank = CoreBank(rw_store)
    before_holding = _holding(rw_store, "wp_bond")
    before_balance = _balance(rw_store, "acc_savings")

    receipt = bank.redeem_product(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        product_id="wp_bond",
        amount=Money(50000),
        idempotency_key="redeem-1",
    )

    assert receipt.holding_before == before_holding
    assert receipt.holding_after == before_holding - 50000
    assert _holding(rw_store, "wp_bond") == before_holding - 50000
    assert _balance(rw_store, "acc_savings") == before_balance + 50000


def test_redeem_more_than_holding_is_rejected_and_writes_nothing(rw_store: Store) -> None:
    """H1：赎回不得超过持仓，且拒绝后余额与持仓都不变（事实零生成）。"""
    bank = CoreBank(rw_store)
    before_holding = _holding(rw_store, "wp_bond")
    before_balance = _balance(rw_store, "acc_savings")
    before_txns = rw_store.query("SELECT COUNT(*) AS n FROM transactions")[0]["n"]

    with pytest.raises(InsufficientHoldingError):
        bank.redeem_product(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            product_id="wp_bond",
            amount=Money(before_holding + 1),
            idempotency_key="redeem-2",
        )

    assert _holding(rw_store, "wp_bond") == before_holding
    assert _balance(rw_store, "acc_savings") == before_balance
    assert rw_store.query("SELECT COUNT(*) AS n FROM transactions")[0]["n"] == before_txns


def test_redeem_without_position_is_rejected(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    with pytest.raises(InsufficientHoldingError):
        bank.redeem_product(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            product_id="wp_mix",
            amount=Money(100),
            idempotency_key="redeem-3",
        )


def test_subscribe_below_min_amount_is_rejected(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    before_balance = _balance(rw_store, "acc_savings")
    with pytest.raises(CoreBankError, match="起购"):
        bank.subscribe_product(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            product_id="wp_mix",
            amount=Money(100000),
            idempotency_key="sub-2",
        )
    assert _balance(rw_store, "acc_savings") == before_balance
    assert _holding(rw_store, "wp_mix") == 0


def test_redeem_replay_returns_same_holding_snapshot(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    first = bank.redeem_product(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        product_id="wp_cash",
        amount=Money(10000),
        idempotency_key="redeem-4",
    )
    replay = bank.redeem_product(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        product_id="wp_cash",
        amount=Money(10000),
        idempotency_key="redeem-4",
    )
    assert replay.replayed is True
    assert replay.holding_before == first.holding_before
    assert replay.holding_after == first.holding_after
