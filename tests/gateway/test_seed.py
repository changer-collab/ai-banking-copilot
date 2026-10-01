from pathlib import Path

from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store


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
    assert store.query("SELECT COUNT(*) AS n FROM transactions")[0]["n"] == 24
    assert store.query("SELECT COUNT(*) AS n FROM payees")[0]["n"] == 5
    store.close()


def test_seed_covers_three_months(seeded_db: Path) -> None:
    store = Store(seeded_db)
    months = store.query(
        "SELECT DISTINCT substr(booked_on, 1, 7) AS m FROM transactions ORDER BY m"
    )
    assert [row["m"] for row in months] == ["2026-07", "2026-08", "2026-09"]
    store.close()


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
    assert len(rows) == 3
    store.close()
