"""scenarios 测试共享夹具：复用 01 的 Store / init_schema / seed_database（与 04 同风格）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema


@pytest.fixture
def gateway(tmp_path: Path) -> Iterator[FactGateway]:
    """建库 + 播种 + 只读事实网关（种子数据可复现）。"""
    db_path = tmp_path / "scenarios.db"
    writer = Store(db_path)
    init_schema(writer)
    seed_database(writer)
    writer.close()
    reader = Store(db_path, StoreMode.READ_ONLY)
    yield FactGateway(reader)
    reader.close()
