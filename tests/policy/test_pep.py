from dataclasses import replace
from datetime import date

import pytest

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.plan import Plan
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.errors import (
    BlockedPayeeError,
    GatewayError,
    InsufficientFundsError,
    SandboxError,
    SandboxTimeout,
    SandboxViolation,
    UnknownSubscriptionError,
)
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.preconditions import WritePreconditionError
from ai_banking_copilot.gateway.sandbox import (
    SandboxLimits,
    SandboxMonitor,
    SandboxResourceLimit,
    SandboxUnavailable,
)
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.policy.audit import (
    KIND_BREAKER,
    KIND_DECISION,
    KIND_EXECUTION,
    AuditLog,
)
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.execution import ExecutionContext
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.policy.pep import (
    ExecResult,
    NotAuthorizedError,
    Pep,
    QueryContext,
    QueryResult,
    _plan_hash,
    manifest_hash,
    manifest_plan,
)
from ai_banking_copilot.policy.tool_hook import ToolHookBlocked
from ai_banking_copilot.tools.registry import (
    ForbiddenParamError,
    ParamValidationError,
    ToolCall,
    ToolRegistry,
    UnknownActionError,
)
from tests.policy.verification_support import (
    issue_policy_write_grant,
    make_test_strong_verification_proof,
)


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


def _execute_confirmed(
    pep: Pep, call: ToolCall, level: Level, key: str, plan: Plan | None = None,
) -> ExecResult:
    """以「用户已确认」的服务端路径执行写动作：先签发一次性凭据，再过 Pre-Tool Hook。

    凭据里的 `strong_verified` 与 `approved_level` 模拟会话层确认后的真实值：
    `RED` 及以上必须先过强验证，否则写前复核会要求重新授权。
    """
    decision = _decision(level)
    frozen = manifest_plan(call) if plan is None else plan
    execution = issue_policy_write_grant(
        pep,
        call,
        decision,
        frozen,
        user_id=DEMO_USER_ID,
        session_id=f"policy-session:{key}",
        execution_id=f"policy-execution:{key}",
        approved_level=level,
    )
    return pep.execute(call, decision, frozen, key, execution=execution)


def test_pure_query_executes_and_is_audited(pep: Pep, audit: AuditLog) -> None:
    call = ToolCall(action="account.balance", params=_query_params())
    result = pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "q1")
    assert result.status == "queried"
    assert result.record_ids == ("accounts:acc_savings",)
    assert "128430.00" in result.message
    assert "审计ID" in result.message
    assert audit.verify_chain() is True
    events = audit.events()
    assert len(events) == 1
    assert events[0].kind == KIND_EXECUTION
    assert events[0].payload["level"] == "GREEN"


def test_query_records_provenance_hash(pep: Pep, audit: AuditLog, gateway: FactGateway) -> None:
    call = ToolCall(action="account.balance", params=_query_params())
    pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "q1")
    provenance = audit.events()[0].payload["provenance"]
    expected = gateway.account("acc_savings").content_hash
    assert provenance == [{"record_id": "accounts:acc_savings", "content_hash": expected}]


def test_query_audit_correlates_identity_and_sandbox_monitor(
    pep: Pep,
    audit: AuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = ToolCall(action="account.balance", params=_query_params())
    handler = pep._query_handlers[call.action]
    monitor = {
        "code_sha256": "a" * 64,
        "limits": {"timeout_s": 2.0, "max_payload_bytes": 4096},
        "duration_ms": 3,
        "exit_code": 0,
        "status": "ok",
    }

    def observed(context: QueryContext) -> QueryResult:
        return replace(handler(context), sandbox_events=(monitor,))

    monkeypatch.setitem(pep._query_handlers, call.action, observed)
    pep.execute(
        call,
        _decision(Level.GREEN),
        manifest_plan(call),
        "q-observed",
        user_id="user-7",
        session_id="session-8",
        execution_id="execution-9",
    )

    event = audit.events()[-1]
    assert event.payload["user_id"] == "user-7"
    assert event.payload["session_id"] == "session-8"
    assert event.payload["execution_id"] == "execution-9"
    assert event.payload["sandbox_events"] == [monitor]


@pytest.mark.parametrize(
    "error_type",
    [SandboxError, SandboxTimeout, SandboxViolation, SandboxResourceLimit, SandboxUnavailable],
)
def test_sandbox_query_failures_return_audited_empty_refusal(
    pep: Pep,
    audit: AuditLog,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[SandboxError],
) -> None:
    call = ToolCall(action="account.balance", params=_query_params())

    def fail_safely(context: QueryContext) -> QueryResult:
        del context
        raise error_type("sensitive sandbox detail")

    monkeypatch.setitem(pep._query_handlers, call.action, fail_safely)
    result = pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "sandbox-error")

    assert result.status == "query_refused"
    assert result.record_ids == ()
    assert "暂不可用" in result.message
    assert "sensitive sandbox detail" not in result.message
    event = audit.events()[-1]
    assert event.payload["status"] == "query_refused"
    assert event.payload["record_ids"] == []
    assert event.payload["provenance"] == []
    assert "sensitive sandbox detail" not in repr(event.payload)


def test_sandbox_error_monitor_is_written_to_query_audit(
    pep: Pep,
    audit: AuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = ToolCall(action="account.balance", params=_query_params())
    monitor = {
        "code_sha256": "b" * 64,
        "limits": {"timeout_s": 2.0, "max_payload_bytes": 4096},
        "duration_ms": 7,
        "exit_code": -1,
        "status": "timeout",
    }

    def fail_with_monitor(context: QueryContext) -> QueryResult:
        del context
        error = SandboxTimeout("sensitive sandbox detail")
        error.monitor = monitor
        raise error

    monkeypatch.setitem(pep._query_handlers, call.action, fail_with_monitor)

    result = pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "sandbox-monitor")

    assert result.status == "query_refused"
    event = audit.events()[-1]
    assert event.payload["sandbox_events"] == [monitor]
    assert "sensitive sandbox detail" not in repr(event.payload)


@pytest.mark.parametrize("status", ["ok", "timeout", "rejected"])
def test_query_context_persists_each_sandbox_monitor_before_returning(
    status: str,
    pep: Pep,
    audit: AuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = ToolCall(action="account.balance", params=_query_params())
    handler = pep._query_handlers[call.action]
    monitor = SandboxMonitor(
        code_sha256="c" * 64,
        limits=SandboxLimits(),
        duration_ms=8,
        exit_code=0 if status == "ok" else -1,
        status=status,
    )

    def observed(context: QueryContext) -> QueryResult:
        assert context.sandbox_monitor is not None
        context.sandbox_monitor(monitor)
        events = audit.events()
        assert len(events) == 1
        assert events[0].payload["event_type"] == "sandbox_execution"
        return handler(context)

    monkeypatch.setitem(pep._query_handlers, call.action, observed)
    pep.execute(
        call, _decision(Level.GREEN), manifest_plan(call), "monitor-sink",
        user_id="user-monitor", session_id="session-monitor", execution_id="exec-monitor",
    )

    events = audit.events()
    assert len(events) == 2
    event = events[0]
    assert event.kind == KIND_EXECUTION
    assert event.timestamp.endswith("Z")
    assert event.payload["user_id"] == "user-monitor"
    assert event.payload["session_id"] == "session-monitor"
    assert event.payload["execution_id"] == "exec-monitor"
    assert event.payload["plan_hash"] == _plan_hash(manifest_plan(call))
    assert event.payload["sandbox_monitor"] == {
        "code_sha256": "c" * 64,
        "limits": {
            field: getattr(monitor.limits, field)
            for field in monitor.limits.__dataclass_fields__
        },
        "duration_ms": 8,
        "exit_code": monitor.exit_code,
        "status": status,
        "isolation": "process",
        "result_operator": None,
        "result_value": None,
        "result_sources": [],
    }
    assert events[1].payload["status"] == "queried"
    assert audit.verify_chain()


def test_query_monitor_audit_failure_blocks_result_use(
    pep: Pep,
    audit: AuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = ToolCall(action="account.balance", params=_query_params())
    result_used = False

    def monitored(context: QueryContext) -> QueryResult:
        nonlocal result_used
        assert context.sandbox_monitor is not None
        context.sandbox_monitor(
            SandboxMonitor(
                code_sha256="d" * 64,
                limits=SandboxLimits(),
                duration_ms=1,
                exit_code=0,
                status="ok",
            )
        )
        result_used = True
        return QueryResult((), (), "不应返回")

    def fail_audit(*args: object, **kwargs: object) -> str:
        raise OSError("sensitive audit storage detail")

    monkeypatch.setitem(pep._query_handlers, call.action, monitored)
    monkeypatch.setattr(audit, "append", fail_audit)

    with pytest.raises(NotAuthorizedError, match="隔离执行审计不可用") as error:
        pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "sink-failure")

    assert result_used is False
    assert "sensitive audit storage detail" not in str(error.value)


def test_query_rejection_keeps_server_identity_without_write_context(
    pep: Pep,
    audit: AuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = ToolCall(action="account.balance", params=_query_params())

    def refuse(_context: QueryContext) -> QueryResult:
        raise NotAuthorizedError("查询监控审计不可用")

    monkeypatch.setitem(pep._query_handlers, call.action, refuse)
    with pytest.raises(NotAuthorizedError):
        pep.execute(
            call, _decision(Level.GREEN), manifest_plan(call), "query-refused",
            user_id="server-user", session_id="server-session", execution_id="server-exec",
        )

    event = audit.events()[-1]
    assert event.payload["reject_reason"] == "query_handler_unavailable"
    assert event.payload["user_id"] == "server-user"
    assert event.payload["session_id"] == "server-session"
    assert event.payload["execution_id"] == "server-exec"


def test_execute_without_decision_is_rejected(pep: Pep, audit: AuditLog) -> None:
    call = ToolCall(action="account.balance", params=_query_params())
    with pytest.raises(NotAuthorizedError, match="无裁决"):
        pep.execute(call, None, manifest_plan(call), "k1")  # type: ignore[arg-type]
    assert audit.events()[-1].kind == KIND_DECISION
    assert audit.events()[-1].payload["reject_reason"] == "missing_decision"


@pytest.mark.parametrize(
    ("case", "expected_error", "reason"),
    [
        ("missing_plan", StalePlanError, "missing_confirmed_plan"),
        ("breaker_open", NotAuthorizedError, "breaker_open"),
        ("green_write", NotAuthorizedError, "green_write_forbidden"),
        ("taint", ForbiddenParamError, "untrusted_control_value"),
        ("parameters", ParamValidationError, "invalid_parameters"),
        ("member", StalePlanError, "plan_member_mismatch"),
    ],
)
def test_execute_early_rejections_are_audited_with_stable_identity(
    case: str,
    expected_error: type[Exception],
    reason: str,
    pep: Pep,
    audit: AuditLog,
    breaker: CircuitBreaker,
) -> None:
    if case in {"missing_plan", "member", "green_write", "breaker_open"}:
        call = ToolCall(action="transfer.create", params=_transfer_params())
        decision = _decision(Level.GREEN if case == "green_write" else Level.YELLOW)
    elif case == "taint":
        call = ToolCall(
            action="account.balance",
            params={"account_id": Value("secret-account", Trust.UNVERIFIED)},
        )
        decision = _decision(Level.GREEN)
    else:
        call = ToolCall(
            action="account.balance",
            params={"account_id": _trusted("acc_savings"), "unexpected": "secret-value"},
        )
        decision = _decision(Level.GREEN)

    plan = manifest_plan(call)
    if case == "missing_plan":
        plan = None  # type: ignore[assignment]
    elif case == "member":
        approved = ToolCall(action="card.lock", params={"card_id": _trusted("card_debit")})
        plan = manifest_plan(approved)
    elif case == "breaker_open":
        breaker.lock()
    execution = ExecutionContext(
        execution_id="exec-audit-test",
        frozen_hash="frozen-audit-test",
        user_id="user-audit-test",
        today=date(2026, 10, 20),
        approved_level=Level.RED,
        strong_verified=True,
        session_id="session-audit-test",
        grant_id="grant-audit-test",
    )

    with pytest.raises(expected_error):
        pep.execute(call, decision, plan, f"audit-{case}", execution=execution)

    event = audit.events()[-1]
    assert event.timestamp.endswith("Z")
    assert event.kind == KIND_DECISION
    assert event.payload["event_type"] == "pep_rejection"
    assert event.payload["status"] == "rejected"
    assert event.payload["reject_reason"] == reason
    assert event.payload["action"] == call.action
    assert event.payload["execution_id"] == "exec-audit-test"
    assert event.payload["session_id"] == "session-audit-test"
    assert event.payload["user_id"] == "user-audit-test"
    assert event.payload["grant_id"] == "grant-audit-test"
    assert event.payload["plan_id"] == (plan.plan_id if isinstance(plan, Plan) else "")
    assert "secret-value" not in repr(event.payload)


def test_rejection_audit_failure_keeps_the_original_refusal_and_never_dispatches(
    pep: Pep,
    audit: AuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    dispatched = False

    def handler(*args: object, **kwargs: object) -> None:
        nonlocal dispatched
        dispatched = True

    def append(*args: object, **kwargs: object) -> str:
        raise OSError("secret audit error")

    monkeypatch.setitem(pep._write_handlers, call.action, handler)
    monkeypatch.setattr(audit, "append", append)
    approved = ToolCall(action="card.lock", params={"card_id": _trusted("card_debit")})

    with pytest.raises(StalePlanError, match="不属于已确认计划") as error:
        pep.execute(call, _decision(Level.RED), manifest_plan(approved), "failed-audit")

    assert dispatched is False
    assert "secret audit error" not in str(error.value)
    assert any("审计链" in note for note in error.value.__notes__)


def test_write_precondition_rejection_is_audited_without_business_facts(
    pep: Pep,
    audit: AuditLog,
    gateway: FactGateway,
) -> None:
    first = ToolCall(action="transfer.create", params=_transfer_params(60_000))
    _execute_confirmed(pep, first, Level.YELLOW, "precondition-first")
    second = ToolCall(action="transfer.create", params=_transfer_params(70_000))
    balance_before = gateway.account("acc_savings").payload.balance.cents

    with pytest.raises(WritePreconditionError):
        _execute_confirmed(pep, second, Level.YELLOW, "precondition-second")

    event = audit.events()[-1]
    assert event.kind == KIND_DECISION
    assert event.payload["reject_reason"] == "write_precondition_reauthorize"
    assert event.payload["execution_id"]
    assert event.payload["grant_id"]
    assert "detail" not in event.payload
    assert "params" not in event.payload
    assert gateway.account("acc_savings").payload.balance.cents == balance_before


def test_manual_and_deny_levels_are_rejected(pep: Pep, audit: AuditLog) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    for level in (Level.MANUAL, Level.DENY):
        with pytest.raises(NotAuthorizedError, match="级别不足"):
            pep.execute(call, _decision(level), manifest_plan(call), f"k-{level.name}")
    assert [event.payload["reject_reason"] for event in audit.events()] == [
        "decision_requires_manual",
        "decision_requires_manual",
    ]


def test_green_cannot_execute_state_change(pep: Pep) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    with pytest.raises(NotAuthorizedError, match="纯查询"):
        pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "k1")


def test_unknown_action_is_rejected(pep: Pep, audit: AuditLog) -> None:
    call = ToolCall(action="payee.delete", params={})
    with pytest.raises(UnknownActionError):
        pep.execute(call, _decision(Level.RED), manifest_plan(call), "k1")
    event = audit.events()[-1]
    assert event.kind == KIND_DECISION
    assert event.payload["reject_reason"] == "unknown_action"
    assert event.payload["action"] == "payee.delete"


def test_call_outside_the_frozen_plan_is_rejected(pep: Pep) -> None:
    """节点级执行门：`call` 必须属于**已确认的冻结计划**（原「单节点自证哈希」已废除）。"""
    approved = ToolCall(action="transfer.create", params=_transfer_params())
    outsider = ToolCall(action="card.lock", params={"card_id": _trusted("card_debit")})
    with pytest.raises(StalePlanError, match="不属于已确认计划"):
        pep.execute(outsider, _decision(Level.YELLOW), manifest_plan(approved), "k1")


def test_missing_frozen_plan_is_rejected(pep: Pep) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    with pytest.raises(StalePlanError, match="缺少已确认的冻结计划"):
        pep.execute(call, _decision(Level.RED), None, "k1")  # type: ignore[arg-type]


def test_untrusted_control_slot_value_is_rejected(pep: Pep) -> None:
    call = ToolCall(
        action="account.balance",
        params={"account_id": Value("acc_savings", Trust.UNVERIFIED)},
    )
    with pytest.raises(ForbiddenParamError, match="unverified"):
        pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "k1")


def test_open_breaker_blocks_writes_but_not_queries(pep: Pep, breaker: CircuitBreaker) -> None:
    breaker.lock()
    query = ToolCall(action="account.balance", params=_query_params())
    result = pep.execute(query, _decision(Level.GREEN), manifest_plan(query), "q1")
    assert result.status == "queried"
    write = ToolCall(action="transfer.create", params=_transfer_params())
    with pytest.raises(NotAuthorizedError, match="熔断"):
        pep.execute(write, _decision(Level.RED), manifest_plan(write), "k1")


def test_repeated_rejections_open_the_breaker(
    pep: Pep, breaker: CircuitBreaker, audit: AuditLog,
) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    for index in range(3):
        with pytest.raises(NotAuthorizedError):
            pep.execute(
                call,
                _decision(Level.DENY),
                manifest_plan(call),
                f"k{index}",
                user_id="user-7",
                session_id="session-8",
                execution_id="execution-9",
            )
    assert breaker.is_open() is True
    breaker_events = [event for event in audit.events() if event.kind == KIND_BREAKER]
    assert len(breaker_events) == 1
    assert breaker_events[0].payload == {
        "trigger": "consecutive_auth_failures",
        "detail": "PEP 失败计数达到全局熔断阈值",
        "scope": "global",
        "state": "open",
        "action": "transfer.create",
        "reject_reason": "decision_requires_manual",
        "user_id": "user-7",
        "session_id": "session-8",
        "execution_id": "execution-9",
    }

    with pytest.raises(NotAuthorizedError):
        pep.execute(
            call,
            _decision(Level.DENY),
            manifest_plan(call),
            "k-repeat",
            user_id="user-7",
            session_id="session-8",
            execution_id="execution-9",
        )
    assert len([event for event in audit.events() if event.kind == KIND_BREAKER]) == 1


def test_pre_tool_hook_breaker_event_keeps_caller_identity(
    pep: Pep,
    breaker: CircuitBreaker,
    audit: AuditLog,
) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    for index in range(3):
        with pytest.raises(ToolHookBlocked):
            pep.execute(
                call,
                _decision(Level.YELLOW),
                manifest_plan(call),
                f"missing-grant-{index}",
                user_id="user-11",
                session_id="session-12",
                execution_id="execution-13",
            )

    assert breaker.is_open() is True
    event = next(event for event in audit.events() if event.kind == KIND_BREAKER)
    assert event.payload["trigger"] == "consecutive_auth_failures"
    assert event.payload["scope"] == "global"
    assert event.payload["state"] == "open"
    assert event.payload["action"] == "transfer.create"
    assert event.payload["reject_reason"] == "write_grant_confirmation_required"
    assert event.payload["user_id"] == "user-11"
    assert event.payload["session_id"] == "session-12"
    assert event.payload["execution_id"] == "execution-13"


def test_breaker_retrip_after_unlock_is_audited_again(
    pep: Pep,
    breaker: CircuitBreaker,
    audit: AuditLog,
) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    for cycle in range(2):
        for index in range(3):
            with pytest.raises(NotAuthorizedError):
                pep.execute(
                    call,
                    _decision(Level.DENY),
                    manifest_plan(call),
                    f"cycle-{cycle}-{index}",
                )
        if cycle == 0:
            assert breaker.is_open() is True
            breaker.unlock()

    breaker_events = [event for event in audit.events() if event.kind == KIND_BREAKER]
    assert len(breaker_events) == 2
    assert all(event.payload["state"] == "open" for event in breaker_events)


def test_account_transactions_query_returns_canonical_records(pep: Pep) -> None:
    call = ToolCall(
        action="account.transactions",
        params={
            "account_id": _trusted("acc_current"),
            "start": _trusted("2026-09-01"),
            "end": _trusted("2026-09-10"),
        },
    )
    result = pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "q2")
    assert result.status == "queried"
    assert result.record_ids == (
        "transactions:txn_20260902",
        "transactions:txn_20260905",
        "transactions:txn_20260908",
    )


def test_bills_category_trend_query_reports_base_period(pep: Pep) -> None:
    call = ToolCall(
        action="bills.category_trend",
        params={
            "user_id": _trusted(DEMO_USER_ID),
            "category": _trusted("购物"),
            "base_year": _trusted(2026),
            "base_month": _trusted(9),
        },
    )
    result = pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "q3")
    assert result.status == "queried"
    assert "购物" in result.message


def test_transfer_settles_and_debits_balance(pep: Pep, rw_store: Store) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    result = _execute_confirmed(pep, call, Level.RED, "k1")
    assert result.status == "settled"
    assert result.record_ids[0].startswith("transactions:")
    rows = rw_store.query("SELECT balance_cents FROM accounts WHERE account_id = 'acc_savings'")
    assert rows[0]["balance_cents"] == 12843000 - 50000


def test_write_audit_start_failure_refuses_before_handler_dispatch(
    pep: Pep,
    audit: AuditLog,
    gateway: FactGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    decision = _decision(Level.RED)
    plan = manifest_plan(call)
    execution = issue_policy_write_grant(
        pep,
        call,
        decision,
        plan,
        user_id=DEMO_USER_ID,
        approved_level=Level.RED,
        session_id="session-start-audit",
        execution_id="execution-start-audit",
    )
    original_handler = pep._write_handlers[call.action]
    dispatched = False

    def spy_handler(context):
        nonlocal dispatched
        dispatched = True
        return original_handler(context)

    def fail_audit(*args: object, **kwargs: object) -> str:
        raise OSError("sensitive audit storage detail")

    monkeypatch.setitem(pep._write_handlers, call.action, spy_handler)
    monkeypatch.setattr(audit, "append", fail_audit)
    balance_before = gateway.account("acc_savings").payload.balance.cents

    with pytest.raises(NotAuthorizedError) as error:
        pep.execute(call, decision, plan, "start-audit-failure", execution=execution)

    assert dispatched is False
    assert "sensitive audit storage detail" not in str(error.value)
    assert gateway.account("acc_savings").payload.balance.cents == balance_before


def test_successful_write_has_start_and_completion_events(
    pep: Pep,
    audit: AuditLog,
) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    extra_call = ToolCall(action="card.lock", params={"card_id": _trusted("card_debit")})
    single_call_plan = manifest_plan(call)
    single_extra_plan = manifest_plan(extra_call)
    plan = Plan(
        plan_id="multi-node-plan",
        nodes=(
            replace(single_call_plan.nodes[0], node_id="transfer-node"),
            replace(single_extra_plan.nodes[0], node_id="card-node"),
        ),
    )

    result = _execute_confirmed(pep, call, Level.RED, "start-and-finish", plan)

    assert result.status == "settled"
    events = audit.events()
    assert len(events) == 2
    assert events[0].kind == KIND_DECISION
    assert events[0].payload["event_type"] == "write_execution_started"
    assert events[0].payload["status"] == "started"
    assert events[1].kind == KIND_EXECUTION
    assert events[1].payload["status"] == result.status
    assert events[0].payload["plan_hash"]
    assert events[0].payload["frozen_hash"] == manifest_hash(call)
    assert events[1].payload["plan_hash"]
    assert events[1].payload["plan_hash"] == events[0].payload["plan_hash"]
    assert events[1].payload["plan_hash"] != manifest_hash(call)
    assert events[1].payload["frozen_hash"] == manifest_hash(call)


def test_execution_context_cannot_replace_grant_risk_snapshot(
    pep: Pep,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    decision = _decision(Level.YELLOW)
    plan = manifest_plan(call)
    issued_today = date(2026, 10, 22)
    issued_flags = frozenset({"device_change"})
    execution = pep.issue_write_grant(
        call,
        decision,
        plan,
        approved_level=Level.YELLOW,
        today=issued_today,
        completed_records=(),
        flags=issued_flags,
    )
    observed: dict[str, object] = {}
    import ai_banking_copilot.policy.pep as pep_module
    from ai_banking_copilot.policy.execution import PlanRisk

    def capture_assessment(*args: object, **kwargs: object) -> object:
        del args
        observed.update(kwargs)
        return PlanRisk(
            decision=decision,
            node_decisions=(decision,),
            limit_cents=100_000,
            accumulated_cents=50_000,
        )

    monkeypatch.setattr(pep_module, "assess_plan", capture_assessment)
    forged = replace(
        execution,
        approved_level=Level.RED,
        strong_verified=True,
        today=date(2099, 1, 1),
        completed_records=("transactions:attacker-chosen",),
        flags=frozenset({"off_hours"}),
    )

    result = pep.execute(call, decision, plan, "grant-snapshot", execution=forged)

    assert result.status == "settled"
    assert observed["today"] == issued_today
    assert observed["completed_records"] == ()
    assert observed["flags"] == issued_flags


def test_write_handler_receives_user_from_grant(
    pep: Pep,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = ToolCall(action="card.lock", params={"card_id": _trusted("card_debit")})
    decision = _decision(Level.YELLOW)
    plan = manifest_plan(call)
    execution = pep.issue_write_grant(
        call, decision, plan, user_id=DEMO_USER_ID, approved_level=Level.YELLOW,
    )
    original_handler = pep._write_handlers[call.action]
    handler_params: dict[str, object] = {}

    def capture_handler(context):
        handler_params.update(context.params)
        return original_handler(context)

    monkeypatch.setitem(pep._write_handlers, call.action, capture_handler)

    result = pep.execute(call, decision, plan, "grant-user", execution=execution)

    assert result.status == "locked"
    assert "user_id" not in call.params
    assert handler_params["user_id"] == DEMO_USER_ID


def test_boolean_cannot_issue_red_write_grant(pep: Pep) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    decision = _decision(Level.RED)

    with pytest.raises(NotAuthorizedError, match="强验证证明"):
        pep.issue_write_grant(
            call,
            decision,
            manifest_plan(call),
            approved_level=Level.RED,
            strong_verified=True,
        )


def test_red_proof_must_match_the_grant_session(pep: Pep) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    decision = _decision(Level.RED)
    plan = manifest_plan(call)
    proof = make_test_strong_verification_proof(
        user_id=DEMO_USER_ID,
        session_id="proof-session",
        execution_id="proof-execution",
        card_id="",
        plan_hash=_plan_hash(plan),
    )

    with pytest.raises(NotAuthorizedError, match="强验证证明"):
        pep.issue_write_grant(
            call,
            decision,
            plan,
            user_id=DEMO_USER_ID,
            session_id="different-session",
            execution_id="proof-execution",
            approved_level=Level.RED,
            verification_proof=proof,
            card_id="",
        )


def test_approved_level_cannot_exceed_decision(pep: Pep) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    decision = _decision(Level.YELLOW)

    with pytest.raises(NotAuthorizedError, match="不能高于"):
        pep.issue_write_grant(
            call,
            decision,
            manifest_plan(call),
            approved_level=Level.RED,
            strong_verified=True,
        )


def test_valid_red_grant_ignores_mutated_execution_approval_fields(pep: Pep) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    decision = _decision(Level.RED)
    plan = manifest_plan(call)
    execution = issue_policy_write_grant(
        pep,
        call,
        decision,
        plan,
        user_id=DEMO_USER_ID,
        session_id="red-grant-session",
        execution_id="red-grant-execution",
        approved_level=Level.RED,
    )
    forged = replace(execution, approved_level=Level.YELLOW, strong_verified=False)

    result = pep.execute(call, decision, plan, "red-grant-context-mutation", execution=forged)

    assert result.status == "settled"


def test_write_records_verified_provenance(
    pep: Pep, audit: AuditLog, gateway: FactGateway
) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    result = _execute_confirmed(pep, call, Level.RED, "k1")
    provenance = audit.events()[-1].payload["provenance"]
    assert len(provenance) == 1
    entry = provenance[0]
    assert entry["record_id"] == result.record_ids[0]
    assert gateway.verify(entry["record_id"], entry["content_hash"]) is True


def test_idempotent_replay_returns_same_receipt(pep: Pep, rw_store: Store) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params())
    frozen = manifest_plan(call)
    first = _execute_confirmed(pep, call, Level.RED, "k1", frozen)
    second = _execute_confirmed(pep, call, Level.RED, "k1", frozen)
    assert second.record_ids == first.record_ids
    assert "回放" in second.message
    rows = rw_store.query(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = '转账' AND channel = 'agent'"
    )
    assert rows[0]["n"] == 1


def test_tampered_amount_fails_execution_gate(pep: Pep) -> None:
    approved = ToolCall(action="transfer.create", params=_transfer_params(amount_cents=50000))
    tampered = ToolCall(action="transfer.create", params=_transfer_params(amount_cents=5000000))
    with pytest.raises(StalePlanError):
        pep.execute(tampered, _decision(Level.RED), manifest_plan(approved), "k1")


def test_subscription_cancel_updates_status(pep: Pep, gateway: FactGateway) -> None:
    call = ToolCall(
        action="subscription.cancel",
        params={
            "subscription_id": _trusted("sub_video"),
            "user_id": _trusted(DEMO_USER_ID),
        },
    )
    result = _execute_confirmed(pep, call, Level.YELLOW, "k1")
    assert result.status == "cancelled"
    assert result.external_status == "not_integrated"
    assert result.previous_status == "active"
    assert gateway.subscription("sub_video").payload.status == "cancelled"


def test_subscription_cancel_refuses_another_users_subscription(
    pep: Pep, rw_store: Store
) -> None:
    """G-01：PEP 只透传服务端身份，底层双条件命中失败 ⇒ 拒绝且不改他人记录。"""
    with rw_store.transaction() as conn:
        conn.execute("INSERT INTO users VALUES ('u_other', '其他用户', 'R3')")
        conn.execute(
            "INSERT INTO subscriptions(subscription_id, user_id, merchant, amount_cents,"
            " period, next_charge_on, status) VALUES(?, ?, ?, ?, ?, ?, ?)",
            ("sub_other", "u_other", "他人商户", 1000, "monthly", "2026-10-20", "active"),
        )
    call = ToolCall(
        action="subscription.cancel",
        params={
            "subscription_id": _trusted("sub_other"),
            "user_id": _trusted(DEMO_USER_ID),
        },
    )
    with pytest.raises(UnknownSubscriptionError):
        _execute_confirmed(pep, call, Level.YELLOW, "k_other")
    rows = rw_store.query(
        "SELECT status FROM subscriptions WHERE subscription_id = 'sub_other'"
    )
    assert rows[0]["status"] == "active"


def test_freeze_then_unfreeze(pep: Pep, gateway: FactGateway) -> None:
    freeze = ToolCall(
        action="funds.freeze",
        params={
            "account_id": _trusted("acc_savings"),
            "amount_cents": _trusted(100000),
            "reason": _trusted("生日预算"),
        },
    )
    held = _execute_confirmed(pep, freeze, Level.RED, "k1")
    hold_id = held.record_ids[0].split(":", 1)[1]
    assert gateway.hold(hold_id).payload.status == "active"
    unfreeze = ToolCall(action="funds.release", params={"hold_id": _trusted(hold_id)})
    released = _execute_confirmed(pep, unfreeze, Level.YELLOW, "k2")
    assert released.status == "released"
    assert gateway.hold(hold_id).payload.status == "released"


def test_wealth_subscribe_requires_min_amount(pep: Pep) -> None:
    call = ToolCall(
        action="wealth.subscribe",
        params={
            "user_id": _trusted(DEMO_USER_ID),
            "account_id": _trusted("acc_savings"),
            "product_id": _trusted("wp_bond"),
            "amount_cents": _trusted(1000),
        },
    )
    with pytest.raises(GatewayError):
        _execute_confirmed(pep, call, Level.RED, "k1")


def test_write_failure_records_audit_and_breaker_failure(
    pep: Pep, audit: AuditLog, breaker: CircuitBreaker
) -> None:
    call = ToolCall(action="transfer.create", params=_transfer_params(amount_cents=99999999))
    with pytest.raises(InsufficientFundsError):
        _execute_confirmed(pep, call, Level.RED, "k1")
    assert breaker.failure_count() == 1
    assert audit.events()[-1].payload["status"] == "failed"
    assert audit.verify_chain() is True


def test_blocked_payee_failure_is_recorded(pep: Pep, breaker: CircuitBreaker) -> None:
    params = _transfer_params()
    params["payee_id"] = _trusted("p_zhaoliu")
    call = ToolCall(action="transfer.create", params=params)
    with pytest.raises(BlockedPayeeError):
        _execute_confirmed(pep, call, Level.RED, "k1")
    assert breaker.failure_count() == 1


CANONICAL_ACTIONS = {
    "account.balance",
    "account.transactions",
    "bills.monthly",
    "bills.category_trend",
    "wealth.list",
    "card.list",
    "subscription.list",
    "transfer.create",
    "transfer.schedule",
    "wealth.subscribe",
    "wealth.redeem",
    "subscription.cancel",
    "subscription.sign",
    "card.lock",
    "card.unlock",
    "card.report_lost",
    "card.apply_virtual",
    "card.set_limit",
    "funds.freeze",
    "funds.release",
}


def test_pep_handles_every_canonical_action(pep: Pep, registry: ToolRegistry) -> None:
    """本测试自建的 20 动作白名单**全部有处理器**（README 裁决 #14；03 独立于 02 的 catalog 验收）。

    步骤 1.5 / 步骤 4「残留」：读、写两侧的分派都已改为**注册表**，`Pep.handled_actions()`
    返回的是**处理器键的并集**（不再拿注册表里的纯查询动作充数）。故此处断言为**包含**
    ——生产侧「处理器集 == 注册表动作集」的双向严格断言在
    `tests/scenarios/test_registry.py::test_every_canonical_action_has_exactly_one_handler`。
    """
    assert len(CANONICAL_ACTIONS) == 20
    assert set(registry.actions()) == CANONICAL_ACTIONS
    assert set(registry.actions()) <= pep.handled_actions()


def test_registered_write_action_without_handler_is_rejected(
    registry: ToolRegistry,
    gateway: FactGateway,
    core_bank: CoreBank,
    audit: AuditLog,
    breaker: CircuitBreaker,
) -> None:
    """fail-closed：写动作在注册表内、但**未登记写处理器** ⇒ 拒绝执行（漏登记即红）。

    步骤 1.5 的核心红线：handler 只在 PEP 的全部前置检查之后被调用；注册表里却没有对应
    handler 时**不能**「什么都不做地放过」，必须显式拒绝。
    """
    pep = Pep(registry, gateway, core_bank, audit, breaker, {}, {})
    call = ToolCall(action="card.lock", params={"card_id": _trusted("card_debit")})
    with pytest.raises(NotAuthorizedError, match="未登记执行处理器"):
        _execute_confirmed(pep, call, Level.YELLOW, "k1")
    assert breaker.failure_count() == 1
    event = audit.events()[-1]
    assert event.kind == KIND_DECISION
    assert event.payload["reject_reason"] == "write_handler_rejected"


def test_registered_query_action_without_handler_is_rejected(
    registry: ToolRegistry,
    gateway: FactGateway,
    core_bank: CoreBank,
    audit: AuditLog,
    breaker: CircuitBreaker,
) -> None:
    """fail-closed：纯查询动作在注册表内、但**未登记读处理器** ⇒ 拒绝执行（对称于写侧）。"""
    pep = Pep(registry, gateway, core_bank, audit, breaker, {}, {})
    call = ToolCall(action="account.balance", params=_query_params())
    with pytest.raises(NotAuthorizedError, match="未登记查询处理器"):
        pep.execute(call, _decision(Level.GREEN), manifest_plan(call), "q1")
    assert breaker.failure_count() == 1
    event = audit.events()[-1]
    assert event.kind == KIND_DECISION
    assert event.payload["reject_reason"] == "query_handler_unavailable"


def test_transfer_schedule_reuses_transfer_semantics(pep: Pep, rw_store: Store) -> None:
    call = ToolCall(action="transfer.schedule", params=_transfer_params())
    result = _execute_confirmed(pep, call, Level.RED, "k2")
    assert result.status == "settled"
    rows = rw_store.query("SELECT COUNT(*) AS n FROM transactions WHERE booked_on = '2026-10-01'")
    assert rows[0]["n"] == 1


def test_card_lock_and_unlock_update_status(pep: Pep, gateway: FactGateway) -> None:
    lock = ToolCall(action="card.lock", params={"card_id": _trusted("card_debit")})
    locked = _execute_confirmed(pep, lock, Level.YELLOW, "k3")
    assert locked.status == "locked"
    assert gateway.card("card_debit").payload.status == "locked"
    unlock = ToolCall(action="card.unlock", params={"card_id": _trusted("card_debit")})
    unlocked = _execute_confirmed(pep, unlock, Level.RED, "k4")
    assert unlocked.status == "active"
    assert gateway.card("card_debit").payload.status == "active"


def test_card_apply_virtual_creates_virtual_card(pep: Pep, gateway: FactGateway) -> None:
    call = ToolCall(
        action="card.apply_virtual",
        params={"user_id": _trusted(DEMO_USER_ID), "daily_limit_cents": _trusted(300000)},
    )
    result = _execute_confirmed(pep, call, Level.YELLOW, "k5")
    assert result.status == "applied"
    card_id = result.record_ids[0].split(":", 1)[1]
    assert gateway.card(card_id).payload.card_type == "virtual"
    assert gateway.card(card_id).payload.daily_limit == Money(300000)


def test_card_set_limit_updates_limit(pep: Pep, gateway: FactGateway) -> None:
    call = ToolCall(
        action="card.set_limit",
        params={"card_id": _trusted("card_debit"), "daily_limit_cents": _trusted(1500000)},
    )
    result = _execute_confirmed(pep, call, Level.YELLOW, "k6")
    assert result.status == "updated"
    assert gateway.card("card_debit").payload.daily_limit == Money(1500000)


def test_subscription_sign_creates_subscription(pep: Pep, gateway: FactGateway) -> None:
    call = ToolCall(
        action="subscription.sign",
        params={
            "user_id": _trusted(DEMO_USER_ID),
            "merchant": _trusted("云盘会员"),
            "amount_cents": _trusted(19800),
            "period": _trusted("yearly"),
            "next_charge_on": _trusted("2027-03-01"),
        },
    )
    result = _execute_confirmed(pep, call, Level.YELLOW, "k7")
    assert result.status == "signed"
    subscription_id = result.record_ids[0].split(":", 1)[1]
    assert gateway.subscription(subscription_id).payload.merchant == "云盘会员"
