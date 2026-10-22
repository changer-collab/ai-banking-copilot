from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import pytest

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    cfhash,
)
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.policy.audit import KIND_DECISION, KIND_EXECUTION, AuditLog
from ai_banking_copilot.policy.breaker import BreakerTrigger, CircuitBreaker
from ai_banking_copilot.policy.gate import StalePlanError, assert_frozen
from ai_banking_copilot.policy.pdp import ObjectState, PolicyRequest, evaluate, evaluate_plan
from ai_banking_copilot.policy.pep import ExecResult, NotAuthorizedError, Pep, manifest_plan
from ai_banking_copilot.policy.saga import ReversalState, ReversalWindow, Saga
from ai_banking_copilot.tools.registry import ToolCall
from tests.policy.verification_support import issue_policy_write_grant


@dataclass(frozen=True)
class _Net:
    amount: Money
    irreversibles: tuple[str, ...] = ()


def _decision(level: Level) -> Decision:
    reasons = (
        (ReasonCode.QUERY_ONLY,)
        if level is Level.GREEN
        else (ReasonCode.STATE_CHANGE_SMALL,)
    )
    return Decision(level=level, reasons=reasons)


def _trusted(value: object) -> Value[object]:
    """控制槽参数必须携带可信来源标记（02 的 `validate_params` 强制，H3）。"""
    return Value(value, Trust.TRUSTED)


def _query_params(account_id: str = "acc_savings") -> dict[str, object]:
    return {"account_id": _trusted(account_id)}


def _transfer_params(amount_cents: int = 50000) -> dict[str, object]:
    return {
        "user_id": _trusted(DEMO_USER_ID),
        "account_id": _trusted("acc_savings"),
        "payee_id": _trusted("p_lina"),
        "amount_cents": _trusted(amount_cents),
        "booked_on": _trusted("2026-10-01"),
    }


def _execute_confirmed(pep: Pep, call: ToolCall, level: Level, key: str) -> ExecResult:
    """以「用户已确认」的服务端路径执行写动作（签发一次性凭据后再过 Pre-Tool Hook）。"""
    decision = _decision(level)
    plan = manifest_plan(call)
    execution = issue_policy_write_grant(
        pep,
        call,
        decision,
        plan,
        user_id=DEMO_USER_ID,
        session_id=f"policy-integration:{key}",
        execution_id=f"execution:{key}",
        approved_level=level,
    )
    return pep.execute(call, decision, plan, key, execution=execution)


def _node_params(amount_cents: int = 50000) -> dict[str, object]:
    """计划节点参数（**不加 taint 包装**：`PlanNode.params` 是计划侧数据，不是工具调用参数）。"""
    return {
        "user_id": DEMO_USER_ID,
        "account_id": "acc_savings",
        "payee_id": "p_lina",
        "amount_cents": amount_cents,
        "booked_on": "2026-10-01",
    }


def _recorder(calls: list[str], tag: str) -> Callable[[], str]:
    def record() -> str:
        calls.append(tag)
        return tag

    return record


def _plan(amount_cents: int = 50000) -> Plan:
    return Plan(
        plan_id="p-cross",
        nodes=(
            PlanNode(
                node_id="n1",
                action="account.balance",
                params={"account_id": "acc_savings"},
                intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="a1"),
            ),
            PlanNode(
                node_id="n2",
                action="transfer.create",
                params=_node_params(amount_cents),
                intent=IntentRef(source=AnchorSource.USER_APPROVED, anchor_id="a2"),
                depends_on=("n1",),
            ),
        ),
    )


def test_plan_gate_accepts_frozen_subgraph_and_rejects_tamper() -> None:
    plan = _plan()
    frozen = cfhash(plan.nodes)
    assert_frozen(plan, frozen)
    tampered = Plan(
        plan_id=plan.plan_id,
        nodes=plan.nodes[:1]
        + (replace(plan.nodes[1], params=_node_params(5000000)),),
    )
    with pytest.raises(StalePlanError):
        assert_frozen(tampered, frozen)


def test_i4_prime_plan_net_effect_threshold() -> None:
    plan = _plan()
    assert evaluate_plan(plan, _Net(amount=Money(50000))).level is Level.YELLOW
    assert evaluate_plan(plan, _Net(amount=Money(500000))).level is Level.RED
    assert evaluate_plan(plan, _Net(amount=Money(0))).level is Level.YELLOW


def test_i1_to_i3_prime_single_node_decision_table() -> None:
    query = PolicyRequest(
        user_id=DEMO_USER_ID,
        action="account.balance",
        amount=Money(0),
        daily_accumulated=Money(0),
        object_state=ObjectState.SELF,
        is_pure_query=True,
        irreversible=False,
    )
    assert evaluate(query).level is Level.GREEN

    over_limit = PolicyRequest(
        user_id=DEMO_USER_ID,
        action="transfer.create",
        amount=Money(150000),
        daily_accumulated=Money(150000),
        object_state=ObjectState.UNKNOWN,
        is_pure_query=False,
        irreversible=False,
    )
    assert evaluate(over_limit).level is Level.RED

    blocked = PolicyRequest(
        user_id=DEMO_USER_ID,
        action="transfer.create",
        amount=Money(100),
        daily_accumulated=Money(100),
        object_state=ObjectState.BLOCKED,
        is_pure_query=False,
        irreversible=False,
    )
    assert evaluate(blocked).level is Level.DENY

    irreversible = PolicyRequest(
        user_id=DEMO_USER_ID,
        action="card.report_lost",
        amount=Money(0),
        daily_accumulated=Money(0),
        object_state=ObjectState.SELF,
        is_pure_query=False,
        irreversible=True,
    )
    assert evaluate(irreversible).level is Level.RED


def test_end_to_end_query_then_transfer_with_intact_audit_chain(
    pep: Pep, audit: AuditLog, gateway: FactGateway
) -> None:
    query_call = ToolCall(action="account.balance", params=_query_params())
    query_result = pep.execute(query_call, _decision(Level.GREEN), manifest_plan(query_call), "q1")
    transfer_call = ToolCall(action="transfer.create", params=_transfer_params())
    transfer_result = _execute_confirmed(pep, transfer_call, Level.RED, "t1")
    assert query_result.status == "queried"
    assert transfer_result.status == "settled"
    assert [event.kind for event in audit.events()] == [
        KIND_EXECUTION, KIND_DECISION, KIND_EXECUTION,
    ]
    assert audit.verify_chain() is True
    transaction_id = transfer_result.record_ids[0]
    txn = gateway.transaction(transaction_id.split(":")[1])
    assert gateway.verify(transaction_id, txn.content_hash) is True


def test_breaker_opens_on_rejections_and_only_unlock_restores(
    pep: Pep, breaker: CircuitBreaker
) -> None:
    for index in range(3):
        call = ToolCall(action="transfer.create", params=_transfer_params())
        with pytest.raises(NotAuthorizedError):
            pep.execute(call, _decision(Level.MANUAL), manifest_plan(call), f"m{index}")
    assert breaker.is_open() is True
    assert breaker.triggers() == (BreakerTrigger.CONSECUTIVE_AUTH_FAILURES,)
    blocked_call = ToolCall(action="transfer.create", params=_transfer_params())
    with pytest.raises(NotAuthorizedError, match="熔断"):
        pep.execute(blocked_call, _decision(Level.RED), manifest_plan(blocked_call), "t1")
    breaker.unlock()
    assert _execute_confirmed(pep, blocked_call, Level.RED, "t2").status == "settled"


def test_reversal_race_never_claims_cancelled() -> None:
    now = datetime(2026, 10, 1, 9, 0, 0)
    calls: list[str] = []
    saga = Saga()
    saga.add("charge", _recorder(calls, "charge"), _recorder(calls, "refund"))
    saga.run()
    window = ReversalWindow.open("sub_video", now=now)
    outcome = window.reverse(now=now, settled=True, compensate=saga.compensate)
    assert outcome.state is ReversalState.COMPENSATING
    assert "已取消" not in outcome.message
    assert "已扣" in outcome.message
    assert outcome.compensated_steps == ("charge",)


def test_tampered_audit_line_is_detected(tmp_path: Path, pep: Pep, audit: AuditLog) -> None:
    call = ToolCall(action="account.balance", params=_query_params())
    pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "q1")
    path = audit.path
    path.write_text(
        path.read_text(encoding="utf-8").replace("queried", "settled"), encoding="utf-8"
    )
    assert audit.verify_chain() is False
