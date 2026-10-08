from pathlib import Path

from ai_banking_copilot.gateway.seed import (
    _SEED_FIRST,
    _SEED_LAST,
    DEMO_USER_ID,
    seed_database,
)
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.scenarios.bills import ytd_end_month
from ai_banking_copilot.scenarios.scheduling import REFERENCE_DATE


def test_seed_creates_demo_user(seeded_db: Path) -> None:
    store = Store(seeded_db)
    rows = store.query(
        "SELECT display_name, risk_profile FROM users WHERE user_id = ?", (DEMO_USER_ID,)
    )
    assert rows[0]["display_name"] == "陈明"
    assert rows[0]["risk_profile"] == "R3"
    store.close()


def test_seed_is_idempotent(seeded_db: Path) -> None:
    store = Store(seeded_db)
    seed_database(store)
    seed_database(store)
    # 覆盖区间 2025-01..2026-09 = 21 个月 × 8 条/月 = 168 条（由 3 个月扩为 21 个月）。
    assert store.query("SELECT COUNT(*) AS n FROM transactions")[0]["n"] == 168
    assert store.query("SELECT COUNT(*) AS n FROM payees")[0]["n"] == 5
    store.close()


def test_seed_tops_up_a_partially_seeded_database(seeded_db: Path) -> None:
    """**补种语义**：种子扩充（新增历史区间）后，既有库会被自动补齐，无需重建库。"""
    store = Store(seeded_db)
    with store.transaction() as conn:
        conn.execute("DELETE FROM transactions WHERE substr(booked_on, 1, 4) = '2025'")
        conn.execute("DELETE FROM schema_meta WHERE key = 'seeded'")
    store.close()

    reopened = Store(seeded_db)
    seed_database(reopened)
    assert reopened.query("SELECT COUNT(*) AS n FROM transactions")[0]["n"] == 168
    reopened.close()


def test_seed_never_overwrites_demo_changes(seeded_db: Path) -> None:
    """**不覆盖真实变更**：演示中改过的余额 / 卡片状态在重复播种后保持不变。"""
    store = Store(seeded_db)
    with store.transaction() as conn:
        conn.execute("UPDATE accounts SET balance_cents = 1 WHERE account_id = 'acc_savings'")
        conn.execute("UPDATE cards SET status = 'locked' WHERE card_id = 'card_debit'")
        conn.execute(
            "INSERT INTO transactions(txn_id, user_id, account_id, booked_on, amount_cents,"
            " merchant, category, channel, direction)"
            " VALUES('txn_demo_flow', 'u_demo', 'acc_savings', '2026-10-01', -100,"
            " '演示转账', '转账', 'app', 'out')"
        )

    seed_database(store)
    assert store.query("SELECT balance_cents FROM accounts WHERE account_id = 'acc_savings'")[0][
        "balance_cents"
    ] == 1
    assert store.query("SELECT status FROM cards WHERE card_id = 'card_debit'")[0]["status"] == (
        "locked"
    )
    assert store.query("SELECT COUNT(*) AS n FROM transactions WHERE txn_id = 'txn_demo_flow'")[0][
        "n"
    ] == 1
    store.close()


def test_seed_covers_full_history_through_baseline(seeded_db: Path) -> None:
    store = Store(seeded_db)
    months = store.query(
        "SELECT DISTINCT substr(booked_on, 1, 7) AS m FROM transactions ORDER BY m"
    )
    assert [row["m"] for row in months] == [
        *(f"2025-{month:02d}" for month in range(1, 13)),
        *(f"2026-{month:02d}" for month in range(1, 10)),
    ]
    store.close()


def test_seed_coverage_end_matches_reference_clock(seeded_db: Path) -> None:
    """种子覆盖的**基准月**必须与唯一基准时钟一致（不得另立第二个时钟）。"""
    assert _SEED_FIRST == (2025, 1)
    assert _SEED_LAST == (REFERENCE_DATE.year, ytd_end_month(REFERENCE_DATE.year))
    assert _SEED_LAST == (2026, 9)


def test_seed_has_ambiguous_name_and_blocked_payee(seeded_db: Path) -> None:
    store = Store(seeded_db)
    same_name = store.query("SELECT COUNT(*) AS n FROM payees WHERE full_name = '王伟'")
    blocked = store.query("SELECT COUNT(*) AS n FROM payees WHERE blocked = 1")
    assert same_name[0]["n"] == 2
    assert blocked[0]["n"] == 1
    store.close()


def test_seed_merchant_text_carries_injection_sample(seeded_db: Path) -> None:
    store = Store(seeded_db)
    rows = store.query("SELECT merchant FROM transactions WHERE merchant LIKE '%忽略以上%'")
    # 该注入样例流水每月 1 条，覆盖区间共 21 个月 ⇒ 21 条。
    assert len(rows) == 21
    store.close()
