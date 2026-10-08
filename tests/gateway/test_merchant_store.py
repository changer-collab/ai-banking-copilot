"""商户下单事实与用户生日资料（`gateway/merchant_store.py` + `CoreBank.place_merchant_order`）。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ai_banking_copilot.gateway.core_bank import CoreBank, WriteReceipt
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.merchant_store import (
    DEMO_BIRTHDAY,
    init_merchant_schema,
    seed_user_profiles,
)
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema


def _db(tmp_path: Path) -> str:
    path = tmp_path / "merchant.db"
    store = Store(path)
    init_schema(store)
    seed_database(store)
    init_merchant_schema(store)
    init_merchant_schema(store)  # 幂等：重复建表不得报错
    seed_user_profiles(store)
    seed_user_profiles(store)  # 幂等：重复播种不得重复插入
    store.close()
    return str(path)


def _order(bank: CoreBank, key: str) -> WriteReceipt:
    return bank.place_merchant_order(
        user_id=DEMO_USER_ID,
        merchant="鲜花礼品店",
        amount=Money(29900),
        order_on=date(2026, 10, 1),
        idempotency_key=key,
    )


def test_place_merchant_order_is_idempotent(tmp_path: Path) -> None:
    rw = Store(_db(tmp_path), StoreMode.READ_WRITE)
    bank = CoreBank(rw)
    first = _order(bank, "k1")
    second = _order(bank, "k1")
    assert first.replayed is False
    assert second.replayed is True
    assert second.record_ids == first.record_ids
    rows = rw.query("SELECT COUNT(*) AS n FROM merchant_orders")
    assert rows[0]["n"] == 1
    rw.close()


def test_seeded_profile_exposes_the_demo_birthday(tmp_path: Path) -> None:
    reader = Store(_db(tmp_path), StoreMode.READ_ONLY)
    profile = FactGateway(reader).user_profile(DEMO_USER_ID)
    assert profile is not None
    assert profile.payload.birthday == DEMO_BIRTHDAY
    reader.close()


def test_user_profile_is_none_for_an_unknown_user(tmp_path: Path) -> None:
    reader = Store(_db(tmp_path), StoreMode.READ_ONLY)
    assert FactGateway(reader).user_profile("u_nobody") is None
    reader.close()
