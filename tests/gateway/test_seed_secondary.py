"""第二演示用户：独立前缀播种，主用户基线逐条不变，且数据互不可见。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.merchant_store import init_merchant_schema
from ai_banking_copilot.gateway.risk_store import init_risk_schema
from ai_banking_copilot.gateway.seed import (
    DEMO_USER_ID,
    SECONDARY_USER_ID,
    seed_database,
    seed_secondary_user,
)
from ai_banking_copilot.gateway.store import Store, init_schema


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    """主用户已播种、且第二用户所需附表均已建好的读写连接。"""

    opened = Store(tmp_path / "demo.db")
    init_schema(opened)
    init_risk_schema(opened)
    init_merchant_schema(opened)
    seed_database(opened)
    yield opened
    opened.close()


def _counts(store: Store) -> dict[str, int]:
    tables = ("users", "accounts", "payees", "cards", "transactions", "subscriptions")
    return {
        table: int(store.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])
        for table in tables
    }


def _owned(store: Store, table: str, user_id: str) -> int:
    rows = store.query(f"SELECT COUNT(*) AS n FROM {table} WHERE user_id = ?", (user_id,))
    return int(rows[0]["n"])


def test_secondary_seed_adds_rows_without_touching_primary(store: Store) -> None:
    before = _counts(store)

    seed_secondary_user(store)

    # 13 个月 × 3 条/月 = 39 条（计数断言用下限，防误删）。
    assert _owned(store, "transactions", SECONDARY_USER_ID) >= 39
    assert _counts(store)["transactions"] >= before["transactions"] + 39
    # 主用户行数一条都没变。
    assert _owned(store, "transactions", DEMO_USER_ID) == before["transactions"]



def test_secondary_seed_is_idempotent(store: Store) -> None:
    seed_secondary_user(store)
    first = _counts(store)

    seed_secondary_user(store)

    assert _counts(store) == first


def test_secondary_user_covers_reference_clock_months(store: Store) -> None:
    seed_secondary_user(store)

    months = store.query(
        "SELECT DISTINCT substr(booked_on, 1, 7) AS m FROM transactions"
        " WHERE user_id = ? ORDER BY m",
        (SECONDARY_USER_ID,),
    )
    assert [row["m"] for row in months] == [
        *(f"2025-{month:02d}" for month in range(10, 13)),
        *(f"2026-{month:02d}" for month in range(1, 11)),
    ]


def test_secondary_seed_leaves_unknown_card_tails_null(store: Store) -> None:
    seed_secondary_user(store)

    tails = store.query(
        "SELECT DISTINCT card_tail FROM transactions WHERE user_id = ?",
        (SECONDARY_USER_ID,),
    )
    assert [row["card_tail"] for row in tails] == [None]


def test_secondary_payees_and_profile_are_distinct(store: Store) -> None:
    seed_secondary_user(store)

    blocked = store.query(
        "SELECT full_name FROM payees WHERE user_id = ? AND blocked = 1", (SECONDARY_USER_ID,)
    )
    assert [row["full_name"] for row in blocked] == ["孙丽"]

    birthday = store.query(
        "SELECT birthday FROM user_profiles WHERE user_id = ?", (SECONDARY_USER_ID,)
    )
    assert birthday[0]["birthday"] == "07-18"


def test_secondary_cards_carry_demo_details(store: Store) -> None:
    """第二用户的卡片页不能出现「卡组织未知 / 限额为空」。"""

    seed_secondary_user(store)

    rows = store.query(
        "SELECT card_id, brand, expiry_month, expiry_year, account_class,"
        " transfer_limit_cents, credit_limit_cents FROM cards WHERE user_id = ?",
        (SECONDARY_USER_ID,),
    )
    cards = {row["card_id"]: row for row in rows}
    debit = cards["card_b_debit"]
    assert debit["brand"] == "unionpay"
    assert (debit["expiry_month"], debit["expiry_year"]) == (6, 2029)
    assert debit["account_class"] == "Ⅰ"
    assert debit["transfer_limit_cents"] == 1_500_000
    credit = cards["card_b_credit"]
    assert credit["brand"] == "visa"
    assert (credit["expiry_month"], credit["expiry_year"]) == (3, 2028)
    assert credit["credit_limit_cents"] == 3_000_000
