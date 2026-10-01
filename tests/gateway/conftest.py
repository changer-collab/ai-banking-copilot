from collections.abc import Iterator
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """建库 + 播种后的数据库文件路径。"""
    db_path = tmp_path / "demo.db"
    store = Store(db_path)
    init_schema(store)
    seed_database(store)
    store.close()
    return db_path


@pytest.fixture
def gateway(seeded_db: Path) -> Iterator[FactGateway]:
    store = Store(seeded_db, StoreMode.READ_ONLY)
    yield FactGateway(store)
    store.close()


@pytest.fixture
def rw_store(seeded_db: Path) -> Iterator[Store]:
    store = Store(seeded_db, StoreMode.READ_WRITE)
    yield store
    store.close()
