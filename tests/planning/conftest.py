from collections.abc import Iterator
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.planning.llm import CANONICAL_ACTIONS


@pytest.fixture
def gateway(tmp_path: Path) -> Iterator[FactGateway]:
    """建库 + 播种 + 只读事实网关（与 01 的 fixture 同构，避免跨测试目录依赖）。"""
    db_path = tmp_path / "planning.db"
    writer = Store(db_path)
    init_schema(writer)
    seed_database(writer)
    writer.close()
    reader = Store(db_path, StoreMode.READ_ONLY)
    yield FactGateway(reader)
    reader.close()


@pytest.fixture
def payees() -> tuple[Payee, ...]:
    """名册快照：含同名双候选（王伟 ×2）与唯一候选（李娜 / 张强）。"""
    return (
        Payee("p_wangwei_a", DEMO_USER_ID, "王伟", "8812", "3301", True, False),
        Payee("p_wangwei_b", DEMO_USER_ID, "王伟", "4417", "7788", False, False),
        Payee("p_lina", DEMO_USER_ID, "李娜", "9021", "2210", True, False),
        Payee("p_zhangqiang", DEMO_USER_ID, "张强", "6633", "5566", False, False),
    )


class StubRegistry:
    """02 未落地前的替身：内核只用到 `actions()`，`ToolRegistry` 结构上即满足。"""

    def __init__(self, actions: tuple[str, ...]) -> None:
        self._actions = actions

    def actions(self) -> tuple[str, ...]:
        return self._actions


@pytest.fixture
def stub_registry() -> StubRegistry:
    return StubRegistry(CANONICAL_ACTIONS)
