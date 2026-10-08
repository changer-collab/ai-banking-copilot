"""补偿通道：真实冲正（`CoreBank.reverse_transfer` + `policy.reversal.ReversalService`）。"""

from datetime import date

import pytest

from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.policy.reversal import ReversalService
from ai_banking_copilot.policy.saga import CompensationError

BOOKED_ON = date(2026, 10, 1)


def _balance(store: Store, account_id: str = "acc_savings") -> int:
    rows = store.query(
        "SELECT balance_cents FROM accounts WHERE account_id = ?", (account_id,)
    )
    return int(rows[0]["balance_cents"])


def _outflow_transfer(core_bank: CoreBank, *, key: str = "t1", amount: int = 50000) -> str:
    receipt = core_bank.transfer(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        payee_id="p_lina",
        amount=Money(amount),
        idempotency_key=key,
        booked_on=BOOKED_ON,
    )
    return receipt.record_ids[0]


def test_reverse_transfer_restores_the_balance(core_bank: CoreBank, rw_store: Store) -> None:
    before = _balance(rw_store)
    txn = _outflow_transfer(core_bank)
    assert _balance(rw_store) == before - 50000
    assert ReversalService(core_bank).compensate((txn,))
    assert _balance(rw_store) == before
    rows = rw_store.query("SELECT direction FROM transactions WHERE category = '冲正'")
    assert [str(row["direction"]) for row in rows] == ["in"]


def test_reverse_transfer_is_idempotent(core_bank: CoreBank, rw_store: Store) -> None:
    """重复冲正同一笔流水不重复入账（沿用 `CoreBank` 幂等键）。"""
    before = _balance(rw_store)
    txn = _outflow_transfer(core_bank)
    service = ReversalService(core_bank)
    first = service.compensate((txn,))
    second = service.compensate((txn,))
    assert second == first
    assert _balance(rw_store) == before
    rows = rw_store.query("SELECT COUNT(*) AS n FROM transactions WHERE category = '冲正'")
    assert rows[0]["n"] == 1


def test_unknown_transaction_is_reported_as_a_failure(core_bank: CoreBank) -> None:
    """流水不存在 ⇒ 如实抛 `CompensationError`，不静默吞掉。"""
    with pytest.raises(CompensationError, match="冲正失败"):
        ReversalService(core_bank).compensate(("transactions:txn_nope",))


def test_records_without_money_impact_are_skipped(core_bank: CoreBank) -> None:
    """纯查询 / 账户类记录没有可回退的资金影响 ⇒ 跳过，不算失败。"""
    service = ReversalService(core_bank)
    assert service.compensate(("records:acc_savings", "accounts:acc_savings")) == ()


def test_inflow_transactions_are_skipped(core_bank: CoreBank) -> None:
    """赎回到账（`direction=in`）没有可冲正的流出资金 ⇒ 跳过，不算失败。"""
    receipt = core_bank.redeem_product(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        product_id="wp_cash",
        amount=Money(10000),
        idempotency_key="redeem-1",
    )
    assert ReversalService(core_bank).compensate(receipt.record_ids) == ()
