"""P2-5 生日联动（`scenarios.cross.birthday_plan`）：公共调度 · 到期判定 · 执行后出处校验。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.plan import Plan
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.merchant_store import init_merchant_schema, seed_user_profiles
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.policy.audit import AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.pep import Pep
from ai_banking_copilot.scenarios.cross import birthday_plan
from ai_banking_copilot.scenarios.registry import QUERY_HANDLERS, WRITE_HANDLERS
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.tools.registry import ToolCall

_DUE = date(2026, 10, 1)
"""生日（演示播种 `10-03`）前 2 天，恰为统一基准日。"""

_BEFORE = date(2026, 9, 30)


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[tuple[FactGateway, Store]]:
    db = tmp_path / "birthday.db"
    writer = Store(db)
    init_schema(writer)
    seed_database(writer)
    init_merchant_schema(writer)
    seed_user_profiles(writer)
    writer.close()
    reader = Store(db, StoreMode.READ_ONLY)
    rw = Store(db, StoreMode.READ_WRITE)
    yield FactGateway(reader), rw
    reader.close()
    rw.close()


def test_birthday_plan_is_due_on_the_lead_day(stores: tuple[FactGateway, Store]) -> None:
    gateway, _ = stores
    nodes = birthday_plan(gateway, _DUE)
    assert [node.node_id for node in nodes] == ["order1"]
    assert nodes[0].action == "merchant.order"
    assert nodes[0].params["order_on"] == _DUE.isoformat()


def test_birthday_plan_is_empty_before_the_lead_day(stores: tuple[FactGateway, Store]) -> None:
    gateway, _ = stores
    assert birthday_plan(gateway, _BEFORE) == ()


def test_birthday_plan_is_empty_without_a_profile(tmp_path: Path) -> None:
    """无生日来源（`user_profiles` 缺行）⇒ 不产出任何节点（不猜）。"""
    db = tmp_path / "noprofile.db"
    writer = Store(db)
    init_schema(writer)
    seed_database(writer)
    init_merchant_schema(writer)
    writer.close()
    reader = Store(db, StoreMode.READ_ONLY)
    try:
        assert birthday_plan(FactGateway(reader), _DUE) == ()
    finally:
        reader.close()


def test_birthday_order_executes_and_verifies_provenance(
    stores: tuple[FactGateway, Store], tmp_path: Path
) -> None:
    """`merchant.order` 执行后须通过出处回读白名单（H1）。"""
    gateway, rw = stores
    nodes = birthday_plan(gateway, _DUE)
    plan = Plan(plan_id="birthday", nodes=nodes)
    pep = Pep(
        build_default_registry(),
        gateway,
        CoreBank(rw),
        AuditLog(tmp_path / "audit.jsonl"),
        CircuitBreaker(),
        WRITE_HANDLERS,
        QUERY_HANDLERS,
    )
    node = nodes[0]
    call = ToolCall(
        action=node.action,
        params={
            "user_id": Value(DEMO_USER_ID, Trust.TRUSTED),
            "merchant": Value(node.params["merchant"], Trust.TRUSTED),
            "amount_cents": Value(node.params["amount_cents"], Trust.TRUSTED),
            "order_on": Value(date.fromisoformat(str(node.params["order_on"])), Trust.TRUSTED),
        },
    )
    result = pep.execute(
        call,
        Decision(level=Level.RED, reasons=(ReasonCode.AMOUNT_OVER_DAILY_LIMIT,)),
        plan,
        "birthday:order1",
    )
    assert result.status == "placed"
    order_id = result.record_ids[0].split(":", 1)[1]
    assert gateway.merchant_order(order_id).payload.status == "placed"
