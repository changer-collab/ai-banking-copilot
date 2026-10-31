"""P2-5 生日联动（`scenarios.cross.birthday_plan`）：公共调度 · 到期判定 · 执行后出处校验。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.plan import Plan, PlanNode
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.errors import CoreBankError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.merchant_store import init_merchant_schema, seed_user_profiles
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.policy.audit import AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.pep import Pep, _plan_hash
from ai_banking_copilot.policy.verification import (
    HmacStrongVerificationProofVerifier,
    VerificationBinding,
    create_challenge,
    verify_challenge,
)
from ai_banking_copilot.scenarios.cross import (
    BIRTHDAY_BUDGET_CENTS,
    BIRTHDAY_ORDER_CENTS,
    birthday_plan,
)
from ai_banking_copilot.scenarios.registry import QUERY_HANDLERS, WRITE_HANDLERS
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.tools.registry import ToolCall

_DUE = date(2026, 10, 31)
"""生日（演示播种 `11-02`）前 2 天，恰为统一基准日。"""

_BEFORE = date(2026, 10, 30)
_TEST_VERIFICATION_KEY = b"birthday-test-verification-key"


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
    assert [node.node_id for node in nodes] == ["freeze1", "order1", "release1"]
    assert [node.action for node in nodes] == [
        "funds.freeze", "merchant.order", "funds.release",
    ]
    freeze, order, release = nodes
    assert freeze.params["amount_cents"] == BIRTHDAY_BUDGET_CENTS
    assert order.params["order_on"] == _DUE.isoformat()
    assert order.params["amount_cents"] == BIRTHDAY_ORDER_CENTS
    assert release.params["amount_cents"] == BIRTHDAY_BUDGET_CENTS - BIRTHDAY_ORDER_CENTS
    assert len({node.params["budget_hold_id"] for node in nodes}) == 1
    assert order.depends_on == ("freeze1",)
    assert release.depends_on == ("freeze1", "order1")


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


def _call_of(node: PlanNode) -> ToolCall:
    """把计划节点还原成 `ToolCall`（控制参数全部标记可信，日期按契约类型还原）。"""
    params: dict[str, object] = {
        key: Value(date.fromisoformat(value) if key == "order_on" else value, Trust.TRUSTED)
        for key, value in node.params.items()
    }
    if node.action == "merchant.order":
        params["user_id"] = Value(DEMO_USER_ID, Trust.TRUSTED)
    return ToolCall(action=node.action, params=params)


def test_birthday_chain_settles_hold_and_releases_the_rest(
    stores: tuple[FactGateway, Store], tmp_path: Path
) -> None:
    """冻结 1000 → 下单 299 → 解冻 701：可用余额只减少 299，冻结记录终态为 `released`。"""
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
        strong_verification=HmacStrongVerificationProofVerifier(_TEST_VERIFICATION_KEY),
    )
    before_cents = gateway.account("acc_savings").payload.balance.cents
    decision = Decision(level=Level.RED, reasons=(ReasonCode.AMOUNT_OVER_DAILY_LIMIT,))
    session_id = "birthday-session"
    execution_id = "birthday-execution"
    card_id = "birthday-card"
    binding = VerificationBinding(
        user_id=DEMO_USER_ID,
        session_id=session_id,
        execution_id=execution_id,
        card_id=card_id,
        frozen_hash=_plan_hash(plan),
    )
    challenge, code = create_challenge(binding, key=_TEST_VERIFICATION_KEY)
    consumed = verify_challenge(challenge, binding, code, key=_TEST_VERIFICATION_KEY)
    proof = HmacStrongVerificationProofVerifier(_TEST_VERIFICATION_KEY).issue(consumed)
    for node in nodes:
        call = _call_of(node)
        result = pep.execute(
            call,
            decision,
            plan,
            f"birthday:{node.node_id}",
            execution=pep.issue_write_grant(
                call,
                decision,
                plan,
                user_id=DEMO_USER_ID,
                session_id=session_id,
                execution_id=execution_id,
                approved_level=Level.RED,
                verification_proof=proof,
                card_id=card_id,
            ),
        )
        assert result.record_ids, node.node_id
        if node.action == "merchant.order":
            order_id = result.record_ids[0].split(":", 1)[1]
            assert gateway.merchant_order(order_id).payload.status == "placed"
    hold = gateway.hold(str(nodes[0].params["budget_hold_id"])).payload
    assert hold.status == "released"
    assert gateway.account("acc_savings").payload.balance.cents == before_cents - (
        BIRTHDAY_ORDER_CENTS
    )


def test_birthday_order_above_frozen_budget_is_refused(stores: tuple[FactGateway, Store]) -> None:
    """冻结额是**硬上限**：下单额超过冻结余额 ⇒ 核心银行拒绝，不落订单。"""
    _, rw = stores
    bank = CoreBank(rw)
    hold_id = "birthday:u_demo:2026:hold"
    bank.freeze_funds(
        account_id="acc_savings",
        amount=Money(BIRTHDAY_BUDGET_CENTS),
        reason="生日预算冻结",
        idempotency_key="birthday-freeze-cap",
        hold_id=hold_id,
    )
    with pytest.raises(CoreBankError):
        bank.place_merchant_order(
            user_id=DEMO_USER_ID,
            merchant="鲜花礼品店",
            amount=Money(BIRTHDAY_BUDGET_CENTS + 1),
            order_on=_DUE,
            idempotency_key="birthday-order-cap",
            occasion_id="birthday:2026:flowers",
            hold_id=hold_id,
        )
    assert not rw.query("SELECT * FROM merchant_orders")
