from datetime import date
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.errors import GatewayNotFoundError, ReadOnlyViolation
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store, StoreMode


def test_fact_gateway_rejects_read_write_store(seeded_db: Path) -> None:
    store = Store(seeded_db)
    with pytest.raises(ReadOnlyViolation):
        FactGateway(store)
    store.close()


def test_record_carries_stable_record_id_and_hash(gateway: FactGateway) -> None:
    record = gateway.account("acc_savings")
    assert record.record_id == "accounts:acc_savings"
    assert record.payload.balance == Money(12843000)
    assert gateway.verify(record.record_id, record.content_hash) is True


def test_verify_rejects_tampered_hash(gateway: FactGateway) -> None:
    record = gateway.account("acc_savings")
    assert gateway.verify(record.record_id, "0" * 64) is False


def test_verify_returns_false_for_unknown_record(gateway: FactGateway) -> None:
    assert gateway.verify("accounts:nope", "0" * 64) is False
    assert gateway.verify("unknown_table:x", "0" * 64) is False


def test_missing_record_raises_not_found(gateway: FactGateway) -> None:
    with pytest.raises(GatewayNotFoundError):
        gateway.payee("p_nope")


def test_monthly_bill_aggregates_by_category(gateway: FactGateway) -> None:
    summary = gateway.monthly_bill(DEMO_USER_ID, 2026, 9)
    totals = {item.category: item.total for item in summary.payload.by_category}
    assert totals == {
        "餐饮": Money(-179200),
        "交通": Money(-27580),
        "住房": Money(-480000),
        "购物": Money(-826000),
        "转账": Money(-6800),
        "生活缴费": Money(-57900),
        "娱乐": Money(-34500),
        "工资": Money(1850000),
    }
    assert summary.payload.total_expense == Money(1611980)
    assert summary.payload.total_income == Money(1850000)
    categories = [item.category for item in summary.payload.by_category]
    assert categories == sorted(categories)


def test_daily_transfer_total_counts_only_outgoing_transfers(gateway: FactGateway) -> None:
    assert gateway.daily_transfer_total(DEMO_USER_ID, date(2026, 9, 11)) == Money(6800)
    assert gateway.daily_transfer_total(DEMO_USER_ID, date(2026, 9, 12)) == Money(0)


def test_transactions_between_filters_by_booked_date(gateway: FactGateway) -> None:
    rows = gateway.transactions_between("acc_current", date(2026, 9, 1), date(2026, 9, 10))
    assert [row.payload.txn_id for row in rows] == [
        "txn_20260902",
        "txn_20260905",
        "txn_20260908",
    ]


def test_transaction_facts_hydrate_booked_at_and_card_tail(gateway: FactGateway) -> None:
    rows = gateway.transactions_between(
        "acc_current", date(2026, 9, 18), date(2026, 9, 18)
    )

    assert len(rows) == 1
    assert rows[0].payload.booked_at == "2026-09-18T20:30:00"
    assert rows[0].payload.card_tail == "4821"


@pytest.mark.parametrize(
    ("card_tail", "expected"),
    [
        ("4821", "4821"),
        ("0007", "0007"),
        (None, None),
        ("", None),
        (" \t", None),
        ("12x4", None),
        ("12345", None),
        ("１２３４", None),
        ("١٢٣٤", None),
    ],
)
def test_transaction_facts_only_expose_valid_card_tail(
    seeded_db: Path, card_tail: str | None, expected: str | None
) -> None:
    writer = Store(seeded_db)
    with writer.transaction() as conn:
        conn.execute(
            "UPDATE transactions SET card_tail = ? WHERE txn_id = 'txn_20260918'",
            (card_tail,),
        )
    writer.close()

    reader = Store(seeded_db, StoreMode.READ_ONLY)
    gateway = FactGateway(reader)
    rows = gateway.transactions_between(
        "acc_current", date(2026, 9, 18), date(2026, 9, 18)
    )

    assert rows[0].payload.card_tail == expected
    reader.close()


def test_collection_readers_return_seeded_cards_and_products(gateway: FactGateway) -> None:
    assert len(gateway.cards_of(DEMO_USER_ID)) == 2
    assert len(gateway.wealth_products()) == 4
    assert len(gateway.subscriptions_of(DEMO_USER_ID)) == 3
    assert len(gateway.payees_of(DEMO_USER_ID)) == 5
