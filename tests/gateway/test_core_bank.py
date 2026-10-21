from datetime import date

import pytest

from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.errors import (
    BlockedPayeeError,
    CoreBankError,
    CurrencyMismatchError,
    IdempotencyConflictError,
    InsufficientFundsError,
    UnknownAccountError,
    UnknownCardError,
    UnknownProductError,
    UnknownSubscriptionError,
)
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store


def _balance(store: Store, account_id: str) -> Money:
    rows = store.query("SELECT balance_cents FROM accounts WHERE account_id = ?", (account_id,))
    return Money(int(rows[0]["balance_cents"]))


def test_transfer_debits_account_and_records_transaction(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    receipt = bank.transfer(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        payee_id="p_lina",
        amount=Money(50000),
        idempotency_key="k1",
        booked_on=date(2026, 10, 1),
    )
    assert receipt.status == "settled"
    assert receipt.replayed is False
    assert _balance(rw_store, "acc_savings") == Money(12843000 - 50000)
    txns = rw_store.query("SELECT COUNT(*) AS n FROM transactions WHERE booked_on = '2026-10-01'")
    assert txns[0]["n"] == 1


def test_transfer_replay_is_idempotent(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    first = bank.transfer(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        payee_id="p_lina",
        amount=Money(50000),
        idempotency_key="k1",
        booked_on=date(2026, 10, 1),
    )
    second = bank.transfer(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        payee_id="p_lina",
        amount=Money(50000),
        idempotency_key="k1",
        booked_on=date(2026, 10, 1),
    )
    assert second.replayed is True
    assert second.record_ids == first.record_ids
    assert _balance(rw_store, "acc_savings") == Money(12843000 - 50000)
    assert rw_store.query("SELECT COUNT(*) AS n FROM write_receipts")[0]["n"] == 1


def test_idempotency_key_cannot_be_reused_for_other_action(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    bank.set_card_status(card_id="card_debit", status="locked", idempotency_key="k9")
    with pytest.raises(IdempotencyConflictError):
        bank.transfer(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            payee_id="p_lina",
            amount=Money(100),
            idempotency_key="k9",
            booked_on=date(2026, 10, 1),
        )


def test_transfer_with_insufficient_funds_leaves_no_side_effect(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    with pytest.raises(InsufficientFundsError):
        bank.transfer(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            payee_id="p_lina",
            amount=Money(99999999),
            idempotency_key="k2",
            booked_on=date(2026, 10, 1),
        )
    assert _balance(rw_store, "acc_savings") == Money(12843000)
    assert rw_store.query("SELECT COUNT(*) AS n FROM write_receipts")[0]["n"] == 0


def test_transfer_to_blocked_payee_is_rejected(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    with pytest.raises(BlockedPayeeError):
        bank.transfer(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            payee_id="p_zhaoliu",
            amount=Money(100),
            idempotency_key="k3",
            booked_on=date(2026, 10, 1),
        )


def test_cancel_subscription_marks_status_cancelled(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    receipt = bank.cancel_subscription(
        subscription_id="sub_video", user_id=DEMO_USER_ID, idempotency_key="k4",
    )
    assert receipt.status == "cancelled"
    assert receipt.external_status == "not_integrated"
    assert receipt.previous_status == "active"
    rows = rw_store.query("SELECT status FROM subscriptions WHERE subscription_id = 'sub_video'")
    assert rows[0]["status"] == "cancelled"


def test_cancel_subscription_refuses_another_users_subscription(rw_store: Store) -> None:
    """G-01：`subscription_id` 命中但归属他人 ⇒ 按「订阅不存在」拒绝，且**不改**记录。"""
    with rw_store.transaction() as conn:
        conn.execute("INSERT INTO users VALUES ('u_other', '其他用户', 'R3')")
        conn.execute(
            "INSERT INTO subscriptions(subscription_id, user_id, merchant, amount_cents,"
            " period, next_charge_on, status) VALUES(?, ?, ?, ?, ?, ?, ?)",
            ("sub_other", "u_other", "他人商户", 1000, "monthly", "2026-10-20", "active"),
        )
    bank = CoreBank(rw_store)
    with pytest.raises(UnknownSubscriptionError):
        bank.cancel_subscription(
            subscription_id="sub_other", user_id=DEMO_USER_ID, idempotency_key="k_other",
        )
    rows = rw_store.query(
        "SELECT status FROM subscriptions WHERE subscription_id = 'sub_other'"
    )
    assert rows[0]["status"] == "active"


def test_cancel_subscription_repeat_is_idempotent_without_new_effects(rw_store: Store) -> None:
    """G-01：重复取消不产生新副作用（第二次 `already_cancelled` 且无 effect）。"""
    bank = CoreBank(rw_store)
    first = bank.cancel_subscription(
        subscription_id="sub_video", user_id=DEMO_USER_ID, idempotency_key="k_first",
    )
    second = bank.cancel_subscription(
        subscription_id="sub_video", user_id=DEMO_USER_ID, idempotency_key="k_second",
    )
    assert first.status == "cancelled"
    assert first.effect_ids
    assert second.status == "already_cancelled"
    assert second.effect_ids == ()
    assert second.previous_status == "cancelled"
    assert second.external_status == "not_integrated"


def test_freeze_then_unfreeze_restores_balance(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    hold = bank.freeze_funds(
        account_id="acc_savings", amount=Money(100000), reason="生日预算", idempotency_key="k5"
    )
    assert _balance(rw_store, "acc_savings") == Money(12843000 - 100000)
    hold_id = hold.effect_ids[0].split(":", 1)[1]
    bank.unfreeze_funds(hold_id=hold_id, idempotency_key="k6")
    assert _balance(rw_store, "acc_savings") == Money(12843000)
    rows = rw_store.query("SELECT status FROM holds WHERE hold_id = ?", (hold_id,))
    assert rows[0]["status"] == "released"


def test_subscribe_product_requires_min_amount(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    with pytest.raises(UnknownProductError):
        bank.subscribe_product(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            product_id="wp_nope",
            amount=Money(1000),
            idempotency_key="k7",
        )
    with pytest.raises(InsufficientFundsError):
        bank.subscribe_product(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            product_id="wp_equity",
            amount=Money(99999999),
            idempotency_key="k8",
        )


def test_redeem_product_credits_account(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    receipt = bank.redeem_product(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        product_id="wp_bond",
        amount=Money(200000),
        idempotency_key="k10",
    )
    assert receipt.status == "settled"
    assert _balance(rw_store, "acc_savings") == Money(12843000 + 200000)


def test_apply_virtual_card_creates_active_virtual_card(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    receipt = bank.apply_virtual_card(
        user_id=DEMO_USER_ID,
        daily_limit=Money(300000),
        idempotency_key="c1",
    )
    assert receipt.status == "applied"
    assert receipt.replayed is False
    card_id = receipt.record_ids[0].split(":", 1)[1]
    rows = rw_store.query(
        "SELECT card_type, status, daily_limit_cents FROM cards WHERE card_id = ?", (card_id,)
    )
    assert rows[0]["card_type"] == "virtual"
    assert rows[0]["status"] == "active"
    assert rows[0]["daily_limit_cents"] == 300000


def test_apply_virtual_card_replay_is_idempotent(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    first = bank.apply_virtual_card(
        user_id=DEMO_USER_ID, daily_limit=Money(300000), idempotency_key="c1"
    )
    second = bank.apply_virtual_card(
        user_id=DEMO_USER_ID, daily_limit=Money(300000), idempotency_key="c1"
    )
    assert second.replayed is True
    assert second.record_ids == first.record_ids
    card_count = rw_store.query("SELECT COUNT(*) AS n FROM cards WHERE card_type = 'virtual'")
    assert card_count[0]["n"] == 1


def test_apply_virtual_card_rejects_negative_limit(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    with pytest.raises(CoreBankError):
        bank.apply_virtual_card(
            user_id=DEMO_USER_ID, daily_limit=Money(-1), idempotency_key="c2"
        )
    assert rw_store.query("SELECT COUNT(*) AS n FROM write_receipts")[0]["n"] == 0


def test_set_card_limit_updates_limit_and_replays(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    first = bank.set_card_limit(
        card_id="card_debit", daily_limit=Money(1500000), idempotency_key="c3"
    )
    assert first.status == "updated"
    rows = rw_store.query("SELECT daily_limit_cents FROM cards WHERE card_id = 'card_debit'")
    assert rows[0]["daily_limit_cents"] == 1500000
    second = bank.set_card_limit(
        card_id="card_debit", daily_limit=Money(1500000), idempotency_key="c3"
    )
    assert second.replayed is True
    assert second.record_ids == first.record_ids


def test_set_card_limit_unknown_card_is_rejected(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    with pytest.raises(UnknownCardError):
        bank.set_card_limit(card_id="card_nope", daily_limit=Money(1000), idempotency_key="c4")


def test_sign_subscription_creates_active_subscription(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    receipt = bank.sign_subscription(
        user_id=DEMO_USER_ID,
        merchant="视频会员",
        amount=Money(2500),
        period="monthly",
        next_charge_on=date(2026, 11, 1),
        idempotency_key="s1",
    )
    assert receipt.status == "signed"
    assert receipt.replayed is False
    subscription_id = receipt.record_ids[0].split(":", 1)[1]
    rows = rw_store.query(
        "SELECT user_id, merchant, amount_cents, period, next_charge_on, status"
        " FROM subscriptions WHERE subscription_id = ?",
        (subscription_id,),
    )
    assert rows[0]["user_id"] == DEMO_USER_ID
    assert rows[0]["merchant"] == "视频会员"
    assert rows[0]["amount_cents"] == 2500
    assert rows[0]["period"] == "monthly"
    assert rows[0]["next_charge_on"] == "2026-11-01"
    assert rows[0]["status"] == "active"


def test_sign_subscription_replay_is_idempotent(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    first = bank.sign_subscription(
        user_id=DEMO_USER_ID,
        merchant="健身会员",
        amount=Money(1200),
        period="monthly",
        next_charge_on=date(2026, 11, 15),
        idempotency_key="s2",
    )
    second = bank.sign_subscription(
        user_id=DEMO_USER_ID,
        merchant="健身会员",
        amount=Money(1200),
        period="monthly",
        next_charge_on=date(2026, 11, 15),
        idempotency_key="s2",
    )
    assert second.replayed is True
    assert second.record_ids == first.record_ids
    count = rw_store.query("SELECT COUNT(*) AS n FROM subscriptions WHERE merchant = '健身会员'")
    assert count[0]["n"] == 1


def test_sign_subscription_rejects_non_positive_amount(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    with pytest.raises(CoreBankError):
        bank.sign_subscription(
            user_id=DEMO_USER_ID,
            merchant="视频会员",
            amount=Money(0),
            period="monthly",
            next_charge_on=date(2026, 11, 1),
            idempotency_key="s3",
        )
    assert rw_store.query("SELECT COUNT(*) AS n FROM write_receipts")[0]["n"] == 0


def test_sign_subscription_rejects_unknown_period(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    with pytest.raises(CoreBankError):
        bank.sign_subscription(
            user_id=DEMO_USER_ID,
            merchant="视频会员",
            amount=Money(2500),
            period="weekly",
            next_charge_on=date(2026, 11, 1),
            idempotency_key="s4",
        )
    assert rw_store.query("SELECT COUNT(*) AS n FROM write_receipts")[0]["n"] == 0


def test_sign_subscription_unknown_user_is_rejected(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    with pytest.raises(CoreBankError):
        bank.sign_subscription(
            user_id="u_nope",
            merchant="视频会员",
            amount=Money(2500),
            period="monthly",
            next_charge_on=date(2026, 11, 1),
            idempotency_key="s5",
        )
    assert rw_store.query("SELECT COUNT(*) AS n FROM write_receipts")[0]["n"] == 0


def _txn_count(store: Store) -> int:
    """仅统计**本次**由 Agent 写入的转账流水（种子数据自带历史流水）。"""
    rows = store.query(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = '转账' AND channel = 'agent'"
    )
    return int(rows[0]["n"])


def test_transfer_rejects_currency_conflicting_with_payer_account(rw_store: Store) -> None:
    """写前币种复核：请求币种必须以付款账户事实为准（fail-closed）。"""
    bank = CoreBank(rw_store)
    with pytest.raises(CurrencyMismatchError):
        bank.transfer(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            payee_id="p_lina",
            amount=Money(50000),
            currency="USD",
            idempotency_key="cur1",
            booked_on=date(2026, 10, 1),
        )
    assert _balance(rw_store, "acc_savings") == Money(12843000)
    assert _txn_count(rw_store) == 0


def test_transfer_rejects_account_without_currency(rw_store: Store) -> None:
    with rw_store.transaction() as conn:
        conn.execute("UPDATE accounts SET currency = '' WHERE account_id = 'acc_savings'")
    with pytest.raises(CurrencyMismatchError):
        CoreBank(rw_store).transfer(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            payee_id="p_lina",
            amount=Money(50000),
            idempotency_key="cur2",
            booked_on=date(2026, 10, 1),
        )
    assert _balance(rw_store, "acc_savings") == Money(12843000)


def test_transfer_accepts_matching_currency_case_insensitively(rw_store: Store) -> None:
    receipt = CoreBank(rw_store).transfer(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        payee_id="p_lina",
        amount=Money(50000),
        currency="cny",
        idempotency_key="cur3",
        booked_on=date(2026, 10, 1),
    )
    assert receipt.status == "settled"


def test_transfer_refuses_another_users_account(rw_store: Store) -> None:
    """无权限账户：`account_id` 归属他人 ⇒ 按「账户不存在」拒绝，不扣款。"""
    with rw_store.transaction() as conn:
        conn.execute("INSERT INTO users VALUES ('u_other', '其他用户', 'R3')")
        conn.execute(
            "INSERT INTO accounts(account_id, user_id, kind, balance_cents, currency)"
            " VALUES('acc_other', 'u_other', 'savings', 100000, 'CNY')"
        )
    with pytest.raises(UnknownAccountError):
        CoreBank(rw_store).transfer(
            user_id=DEMO_USER_ID,
            account_id="acc_other",
            payee_id="p_lina",
            amount=Money(100),
            idempotency_key="own1",
            booked_on=date(2026, 10, 1),
        )
    assert _txn_count(rw_store) == 0


def test_frozen_funds_make_the_account_untransferable(rw_store: Store) -> None:
    """冻结：`holds` 占用可用余额 ⇒ 转账因余额不足失败，不会**超发**。"""
    bank = CoreBank(rw_store)
    bank.freeze_funds(
        account_id="acc_savings",
        amount=Money(12843000),
        reason="司法冻结",
        idempotency_key="hold1",
    )
    with pytest.raises(InsufficientFundsError):
        bank.transfer(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            payee_id="p_lina",
            amount=Money(100),
            idempotency_key="hold2",
            booked_on=date(2026, 10, 1),
        )
    assert _txn_count(rw_store) == 0
    holds = rw_store.query("SELECT status FROM holds")
    assert [row["status"] for row in holds] == ["active"]


def test_duplicate_transfer_submission_adds_no_ledger_row(rw_store: Store) -> None:
    bank = CoreBank(rw_store)
    for _ in range(2):
        bank.transfer(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            payee_id="p_lina",
            amount=Money(50000),
            idempotency_key="dup1",
            booked_on=date(2026, 10, 1),
        )
    assert _txn_count(rw_store) == 1
    assert _balance(rw_store, "acc_savings") == Money(12843000 - 50000)


def test_book_transfer_rejects_currency_conflict_without_scheduling(rw_store: Store) -> None:
    with pytest.raises(CurrencyMismatchError):
        CoreBank(rw_store).book_transfer(
            user_id=DEMO_USER_ID,
            account_id="acc_savings",
            payee_id="p_lina",
            amount=Money(10000),
            booked_on=date(2026, 10, 1),
            remark="",
            idempotency_key="book_cur",
            currency="EUR",
        )
    assert rw_store.query("SELECT COUNT(*) AS n FROM write_receipts")[0]["n"] == 0

