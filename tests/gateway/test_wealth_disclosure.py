"""理财披露事实：完整性、孰高、缺费率不可售。"""

from __future__ import annotations

from datetime import date

from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.models import WealthDisclosure
from ai_banking_copilot.gateway.seed import seed_database
from ai_banking_copilot.gateway.store import Store, init_schema
from ai_banking_copilot.gateway.wealth_disclosure_store import (
    init_wealth_disclosure_schema,
    seed_wealth_disclosures,
    wealth_disclosure,
    wealth_disclosures,
)


def _store(tmp_path) -> Store:  # type: ignore[no-untyped-def]
    store = Store(tmp_path / "demo.db")
    init_schema(store)
    seed_database(store)
    init_wealth_disclosure_schema(store)
    seed_wealth_disclosures(store)
    return store


def test_every_seeded_product_has_complete_disclosure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    try:
        rows = wealth_disclosures(store)
        assert len(rows) == 5
        for disclosure in rows:
            assert disclosure.disclosure_complete is True
            assert disclosure.registration_code.startswith("DEMO-")
            assert disclosure.increment_cents > 0
    finally:
        store.close()


def test_higher_risk_rating_wins(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = _store(tmp_path)
    try:
        mix = wealth_disclosure(store, "wp_mix")
        assert mix is not None
        assert mix.issuer_risk_level == "R2"
        assert mix.seller_risk_level == "R3"
        assert mix.display_risk_level == "R3"
    finally:
        store.close()


def test_missing_fee_means_not_saleable() -> None:
    disclosure = WealthDisclosure(
        product_id="wp_demo",
        product_type="bond",
        registration_code="DEMO-X",
        issuer="演示发行方",
        issuer_risk_level="R2",
        seller_risk_level="R2",
        subscription_fee_bp=0,
        service_fee_bp=None,
        custody_fee_bp=2,
        increment_cents=100000,
        redeem_mode="t_plus_1",
        fast_redeem_limit_cents=None,
        closed_days=30,
        next_open_date=date(2026, 12, 30),
        purchase_cutoff="15:00",
        sale_status="on_sale",
        updated_on=DEFAULT_BOOKING_DATE,
    )
    assert disclosure.disclosure_complete is False
