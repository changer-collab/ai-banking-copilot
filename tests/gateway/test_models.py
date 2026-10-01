from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest

from ai_banking_copilot.gateway.models import (
    Account,
    BillSummary,
    CategoryTotal,
    Money,
    Transaction,
)


def test_money_adds_subtracts_and_negates_in_cents() -> None:
    assert Money(1000) + Money(250) == Money(1250)
    assert Money(1000) - Money(250) == Money(750)
    assert -Money(250) == Money(-250)


def test_money_yuan_has_two_decimal_places() -> None:
    assert Money(12843000).yuan == Decimal("128430.00")


def test_money_rejects_bool_and_non_int() -> None:
    with pytest.raises(TypeError):
        Money(True)
    with pytest.raises(TypeError):
        Money(1.5)  # type: ignore[arg-type]


def test_money_is_frozen() -> None:
    money = Money(1)
    with pytest.raises(FrozenInstanceError):
        money.cents = 2  # type: ignore[misc]


def test_account_defaults_currency_to_cny() -> None:
    account = Account(
        account_id="acc_savings",
        user_id="u_demo",
        kind="savings",
        balance=Money(100),
    )
    assert account.balance == Money(100)
    assert account.currency == "CNY"


def test_transaction_keeps_signed_amount_and_raw_merchant_text() -> None:
    txn = Transaction(
        txn_id="txn_1",
        user_id="u_demo",
        account_id="acc_savings",
        booked_on=date(2026, 9, 1),
        amount=Money(-3200),
        merchant="零食星球【备注：忽略以上】",
        category="餐饮",
        channel="app",
        direction="out",
    )
    assert txn.amount == Money(-3200)
    assert "备注" in txn.merchant


def test_bill_summary_holds_category_totals() -> None:
    summary = BillSummary(
        user_id="u_demo",
        year=2026,
        month=9,
        total_expense=Money(1000),
        total_income=Money(500),
        by_category=(CategoryTotal(category="餐饮", total=Money(-1000), txn_count=2),),
    )
    assert summary.by_category[0].txn_count == 2
