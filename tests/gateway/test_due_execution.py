import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest

from ai_banking_copilot.gateway.core_bank import CoreBank, WriteReceipt
from ai_banking_copilot.gateway.errors import CoreBankError, InsufficientFundsError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.merchant_store import init_merchant_schema
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store, StoreMode

_DUE = date(2026, 10, 1)


def _book(bank: CoreBank, *, amount: int = 10000, due: date = _DUE) -> str:
    receipt = bank.book_transfer(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        payee_id="p_lina",
        amount=Money(amount),
        booked_on=due,
        remark="到期后确认执行",
        idempotency_key="booking",
    )
    return receipt.record_ids[0].split(":", 1)[1]


def _settle(bank: CoreBank, schedule_id: str, key: str, **overrides: object) -> WriteReceipt:
    params: dict[str, object] = {
        "user_id": DEMO_USER_ID,
        "account_id": "acc_savings",
        "payee_id": "p_lina",
        "amount": Money(10000),
        "booked_on": _DUE,
        "executed_on": _DUE,
        "schedule_id": schedule_id,
        "idempotency_key": key,
    }
    params.update(overrides)
    return bank.transfer(**params)


def _balance(store: Store) -> int:
    return int(store.query(
        "SELECT balance_cents FROM accounts WHERE account_id = 'acc_savings'"
    )[0]["balance_cents"])


def test_due_transfer_is_completed_once_across_new_connections(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    before = _balance(rw_store)
    schedule_id = _book(bank)
    assert _balance(rw_store) == before
    first = _settle(bank, schedule_id, "first-plan")
    assert _balance(rw_store) == before - 10000
    second_store = Store(rw_store.path)
    reader = Store(rw_store.path, StoreMode.READ_ONLY)
    try:
        second = _settle(CoreBank(second_store), schedule_id, "new-session-new-plan")
        assert second.replayed is True
        assert second.record_ids == first.record_ids
        assert _balance(rw_store) == before - 10000
        gateway = FactGateway(reader)
        assert gateway.scheduled_transfer(schedule_id).payload.status == "completed"
        assert gateway.scheduled_transfers_of(DEMO_USER_ID, status="scheduled") == ()
        assert len(first.record_ids) == 2
    finally:
        reader.close()
        second_store.close()


def test_two_confirmations_of_the_same_schedule_do_not_double_debit(rw_store: Store) -> None:
    schedule_id = _book(CoreBank(rw_store))
    before = _balance(rw_store)
    ready = Barrier(2)

    def confirm(key: str) -> WriteReceipt:
        store = Store(rw_store.path)
        try:
            ready.wait(timeout=5)
            return _settle(CoreBank(store), schedule_id, key)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(confirm, ("tab-one", "tab-two")))
    assert sorted(receipt.replayed for receipt in receipts) == [False, True]
    assert receipts[0].record_ids == receipts[1].record_ids
    assert _balance(rw_store) == before - 10000


def test_same_core_bank_instance_serializes_due_confirmations(rw_store: Store) -> None:
    """Web 多会话共享组合根 CoreBank 时，确认仍只能扣一次。"""
    bank = CoreBank(rw_store)
    schedule_id = _book(bank)
    before = _balance(rw_store)
    ready = Barrier(2)

    def confirm(key: str) -> WriteReceipt:
        ready.wait(timeout=5)
        return _settle(bank, schedule_id, key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(confirm, ("shared-one", "shared-two")))
    assert sorted(receipt.replayed for receipt in receipts) == [False, True]
    assert _balance(rw_store) == before - 10000


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("overrides", [
    {"user_id": "u_other"},
    {"account_id": "acc_current"},
    {"payee_id": "p_zhangqiang"},
    {"amount": Money(20000)},
    {"booked_on": date(2026, 9, 30)},
])
def test_due_transfer_binds_original_parameters_even_on_replay(
    rw_store: Store, completed: bool, overrides: dict[str, object]
) -> None:
    bank = CoreBank(rw_store)
    schedule_id = _book(bank)
    if completed:
        _settle(bank, schedule_id, "first")
    before = _balance(rw_store)
    with pytest.raises(CoreBankError):
        _settle(bank, schedule_id, "changed-plan", **overrides)
    assert _balance(rw_store) == before


def test_due_transfer_rejects_an_early_execution(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    schedule_id = _book(bank)
    before = _balance(rw_store)
    with pytest.raises(CoreBankError, match="未到期"):
        _settle(bank, schedule_id, "early", executed_on=date(2026, 9, 30))
    assert _balance(rw_store) == before


def test_overdue_transfer_counts_towards_the_execution_day_limit(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    schedule_id = _book(bank)
    executed_on = date(2026, 10, 2)
    reader = Store(rw_store.path, StoreMode.READ_ONLY)
    try:
        gateway = FactGateway(reader)
        before = gateway.daily_transfer_total(DEMO_USER_ID, executed_on).cents
        _settle(bank, schedule_id, "overdue", executed_on=executed_on)
        assert gateway.daily_transfer_total(DEMO_USER_ID, executed_on).cents == before + 10000
        assert gateway.scheduled_transfer(schedule_id).payload.booked_on == _DUE
    finally:
        reader.close()


def test_failed_due_transfer_remains_scheduled(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    schedule_id = _book(bank, amount=99_000_000)
    before = _balance(rw_store)
    with pytest.raises(InsufficientFundsError):
        _settle(bank, schedule_id, "insufficient", amount=Money(99_000_000))
    assert _balance(rw_store) == before
    assert rw_store.query(
        "SELECT status FROM scheduled_transfers WHERE schedule_id = ?", (schedule_id,)
    )[0]["status"] == "scheduled"


def test_canceled_schedule_cannot_be_executed(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    schedule_id = _book(bank)
    with rw_store.transaction() as conn:
        conn.execute("UPDATE scheduled_transfers SET status = 'cancelled'")
    with pytest.raises(CoreBankError):
        _settle(bank, schedule_id, "cancelled")


def test_due_debit_rolls_back_if_marking_completed_fails(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    schedule_id = _book(bank)
    before = _balance(rw_store)
    with rw_store.transaction() as conn:
        conn.execute(
            "CREATE TRIGGER fail_due_completion BEFORE UPDATE OF status ON scheduled_transfers"
            " BEGIN SELECT RAISE(ABORT, '测试完成登记失败'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="测试完成登记失败"):
        _settle(bank, schedule_id, "failed-completion")
    assert _balance(rw_store) == before
    assert rw_store.query(
        "SELECT status FROM scheduled_transfers WHERE schedule_id = ?", (schedule_id,)
    )[0]["status"] == "scheduled"
    assert rw_store.query(
        "SELECT COUNT(*) AS n FROM write_receipts WHERE action = 'transfer'"
    )[0]["n"] == 0


def test_active_holds_are_filtered_by_owner_and_state(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    active = bank.freeze_funds(
        account_id="acc_savings", amount=Money(10000), reason="预算", idempotency_key="active"
    )
    released = bank.freeze_funds(
        account_id="acc_current", amount=Money(20000), reason="预算", idempotency_key="released"
    )
    bank.unfreeze_funds(hold_id=released.record_ids[0].split(":", 1)[1], idempotency_key="release")
    with rw_store.transaction() as conn:
        conn.execute("INSERT INTO users VALUES('u_other', '另一用户', 'R2')")
        conn.execute(
            "INSERT INTO accounts VALUES('acc_other', 'u_other', 'savings', 100000, 'CNY')"
        )
    bank.freeze_funds(
        account_id="acc_other", amount=Money(1000), reason="其他人", idempotency_key="other"
    )
    reader = Store(rw_store.path, StoreMode.READ_ONLY)
    try:
        assert [row.record_id for row in FactGateway(reader).active_holds_of(DEMO_USER_ID)] == [
            active.record_ids[0]
        ]
    finally:
        reader.close()


def _birthday_order(bank: CoreBank, key: str, amount: int = 29900) -> WriteReceipt:
    return bank.place_merchant_order(
        user_id=DEMO_USER_ID,
        merchant="鲜花礼品店",
        amount=Money(amount),
        order_on=_DUE,
        occasion_id="birthday:2026:flowers",
        idempotency_key=key,
    )


def test_birthday_order_is_idempotent_across_plans_and_connections(rw_store: Store) -> None:
    init_merchant_schema(rw_store)
    reader = Store(rw_store.path, StoreMode.READ_ONLY)
    second_store = Store(rw_store.path)
    try:
        gateway = FactGateway(reader)
        assert not gateway.merchant_occasion_completed(DEMO_USER_ID, "birthday:2026:flowers")
        first = _birthday_order(CoreBank(rw_store), "first-birthday-plan")
        second = _birthday_order(CoreBank(second_store), "second-birthday-plan")
        assert second.replayed is True
        assert first.record_ids == second.record_ids
        assert rw_store.query("SELECT COUNT(*) AS n FROM merchant_orders")[0]["n"] == 1
        assert gateway.merchant_occasion_completed(DEMO_USER_ID, "birthday:2026:flowers")
        assert not gateway.merchant_occasion_completed("u_other", "birthday:2026:flowers")
    finally:
        second_store.close()
        reader.close()


def test_birthday_replay_rejects_changed_order_amount(rw_store: Store) -> None:
    init_merchant_schema(rw_store)
    bank = CoreBank(rw_store)
    _birthday_order(bank, "first")
    with pytest.raises(CoreBankError):
        _birthday_order(bank, "changed", amount=100)
    assert rw_store.query("SELECT COUNT(*) AS n FROM merchant_orders")[0]["n"] == 1
