from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from pathlib import Path
from threading import Barrier

import pytest

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.plan import Plan
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE, CoreBank
from ai_banking_copilot.gateway.errors import GatewayError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.preconditions import PreconditionStatus, WritePreconditionError
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store, StoreMode
from ai_banking_copilot.policy.audit import AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.breaker_store import init_breaker_schema
from ai_banking_copilot.policy.execution import CommittedWriteError, ExecutionContext, assess_plan
from ai_banking_copilot.policy.pep import ExecResult, Pep, manifest_plan
from ai_banking_copilot.scenarios.registry import QUERY_HANDLERS, WRITE_HANDLERS
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.tools.registry import ToolCall, ToolRegistry


def _call(cents: int, booked_on: date = DEFAULT_BOOKING_DATE) -> ToolCall:
    return ToolCall("transfer.create", {
        "user_id": Value(DEMO_USER_ID, Trust.TRUSTED),
        "account_id": Value("acc_savings", Trust.TRUSTED),
        "payee_id": Value("p_lina", Trust.TRUSTED),
        "amount_cents": Value(cents, Trust.TRUSTED),
        "booked_on": Value(booked_on.isoformat(), Trust.TRUSTED),
    })


def _decision(level: Level) -> Decision:
    return Decision(level, (ReasonCode.STATE_CHANGE_SMALL,))


def _grant(
    pep: Pep,
    call: ToolCall,
    level: Level,
    *,
    today: date = DEFAULT_BOOKING_DATE,
    execution_id: str = "",
    plan: Plan | None = None,
) -> ExecutionContext:
    """签发一次性写凭据（模拟会话层确认后的真实路径）。

    `RED` 及以上视为已过强验证，否则写前复核会要求重新授权。
    """
    decision = _decision(level)
    frozen = manifest_plan(call) if plan is None else plan
    return pep.issue_write_grant(
        call,
        decision,
        frozen,
        approved_level=level,
        strong_verified=level >= Level.RED,
        today=today,
        execution_id=execution_id,
    )


def _run(pep: Pep, call: ToolCall, level: Level, key: str) -> ExecResult:
    """以已确认路径执行写动作（凭据逐次签发、消费一次）。"""
    return pep.execute(
        call, _decision(level), manifest_plan(call), key,
        execution=_grant(pep, call, level),
    )


def test_two_yellow_confirmations_serialize_at_write(
    pep: Pep, gateway: FactGateway,
) -> None:
    first = _call(60_000)
    second = _call(70_000)
    _run(pep, first, Level.YELLOW, "first-yellow")
    before = gateway.daily_transfer_total(DEMO_USER_ID, DEFAULT_BOOKING_DATE).cents
    with pytest.raises(WritePreconditionError) as error:
        _run(pep, second, Level.YELLOW, "second-yellow")
    assert error.value.result.status is PreconditionStatus.REAUTHORIZE
    assert error.value.result.level == Level.RED
    assert error.value.result.accumulated_cents == 130_000
    assert gateway.daily_transfer_total(DEMO_USER_ID, DEFAULT_BOOKING_DATE).cents == before


def test_legacy_non_default_day_uses_actual_booking_day(
    pep: Pep, gateway: FactGateway,
) -> None:
    booking_day = date(2026, 10, 2)
    first, second = _call(60_000, booking_day), _call(70_000, booking_day)
    _run(pep, first, Level.YELLOW, "next-day-a")
    with pytest.raises(WritePreconditionError) as error:
        _run(pep, second, Level.YELLOW, "next-day-b")
    assert error.value.result.status is PreconditionStatus.REAUTHORIZE
    assert error.value.result.accumulated_cents == 130_000
    assert gateway.daily_transfer_total(DEMO_USER_ID, booking_day).cents == 60_000


def test_explicit_execution_clock_cannot_override_booking_day(
    pep: Pep, gateway: FactGateway,
) -> None:
    booking_day = date(2026, 10, 2)
    first = _call(60_000, booking_day)
    _run(pep, first, Level.YELLOW, "explicit-first")
    call = _call(70_000, booking_day)
    plan = manifest_plan(call)
    execution = _grant(
        pep, call, Level.YELLOW, today=DEFAULT_BOOKING_DATE, execution_id="wrong-day"
    )
    with pytest.raises(WritePreconditionError) as error:
        pep.execute(call, _decision(Level.YELLOW), plan, "wrong-day", execution=execution)
    assert error.value.result.status is PreconditionStatus.REAUTHORIZE
    assert error.value.result.accumulated_cents == 130_000
    assert gateway.daily_transfer_total(DEMO_USER_ID, booking_day).cents == 60_000


def test_multi_day_plan_checks_each_actual_booking_day(
    pep: Pep, gateway: FactGateway,
) -> None:
    first = _call(20_000, date(2026, 10, 2))
    second = _call(20_000, date(2026, 10, 3))
    prior = _call(90_000, date(2026, 10, 3))
    _run(pep, prior, Level.YELLOW, "multi-prior")
    plan = Plan("multi-day", (
        replace(manifest_plan(first).nodes[0], node_id="a"),
        replace(manifest_plan(second).nodes[0], node_id="b"),
    ))
    with pytest.raises(WritePreconditionError) as error:
        pep.execute(
            first, _decision(Level.YELLOW), plan, "multi-day-a",
            execution=_grant(pep, first, Level.YELLOW, plan=plan),
        )
    assert error.value.result.status is PreconditionStatus.REAUTHORIZE
    assert error.value.result.accumulated_cents == 130_000
    assert gateway.daily_transfer_total(DEMO_USER_ID, date(2026, 10, 2)).cents == 0


def test_due_transfer_uses_execution_day_for_risk(
    pep: Pep, gateway: FactGateway,
) -> None:
    execution_day = date(2026, 10, 2)
    prior = _call(60_000, execution_day)
    _run(pep, prior, Level.YELLOW, "due-prior")
    due = replace(manifest_plan(_call(70_000)).nodes[0], params={
        "account_id": "acc_savings", "payee_id": "p_lina",
        "amount_cents": 70_000,
        "booked_on": DEFAULT_BOOKING_DATE.isoformat(),
        "schedule_id": "sched-test", "executed_on": execution_day.isoformat(),
    })
    risk = assess_plan(
        Plan("due", (due,)), registry=build_default_registry(), gateway=gateway,
        user_id=DEMO_USER_ID, today=DEFAULT_BOOKING_DATE,
    )
    assert risk.accumulated_cents == 130_000
    assert risk.decision.level is Level.RED


def test_legacy_yellow_unlock_requires_red_without_write(
    pep: Pep, gateway: FactGateway,
) -> None:
    call = ToolCall("card.unlock", {"card_id": Value("card_debit", Trust.TRUSTED)})
    before = gateway.card("card_debit").payload.status
    with pytest.raises(WritePreconditionError) as error:
        _run(pep, call, Level.YELLOW, "legacy-yellow-unlock")
    assert error.value.result.status is PreconditionStatus.REAUTHORIZE
    assert error.value.result.level == Level.RED
    assert gateway.card("card_debit").payload.status == before


def test_redeem_does_not_offset_positive_plan_outflow(
    registry: ToolRegistry, gateway: FactGateway,
) -> None:
    transfer = replace(manifest_plan(_call(110_000)).nodes[0], node_id="out")
    redeem = replace(transfer, node_id="in", action="wealth.redeem", params={
        "account_id": "acc_savings", "product_id": "wp_stable", "amount_cents": 100_000,
    })
    plan = Plan("gross-outflow", (transfer, redeem))
    risk = assess_plan(
        plan, registry=registry, gateway=gateway, user_id=DEMO_USER_ID,
        today=DEFAULT_BOOKING_DATE,
    )
    assert risk.accumulated_cents == 110_000
    assert risk.decision.level is Level.RED


def test_plan_positive_outflow_and_completed_transfer_are_counted_once(
    pep: Pep, registry: ToolRegistry, gateway: FactGateway,
) -> None:
    first, second = _call(60_000), _call(70_000)
    a = replace(manifest_plan(first).nodes[0], node_id="a")
    b = replace(manifest_plan(second).nodes[0], node_id="b")
    plan = Plan("two-transfers", (a, b))
    initial = assess_plan(
        plan, registry=registry, gateway=gateway, user_id=DEMO_USER_ID,
        today=DEFAULT_BOOKING_DATE,
    )
    assert initial.decision.level is Level.RED
    assert initial.accumulated_cents == 130_000
    receipt = pep.execute(
        first, _decision(Level.RED), plan, "same-execution:a",
        execution=_grant(pep, first, Level.RED, plan=plan, execution_id="same-execution"),
    )
    remaining = assess_plan(
        plan, registry=registry, gateway=gateway, user_id=DEMO_USER_ID,
        today=DEFAULT_BOOKING_DATE, completed_records=receipt.record_ids,
    )
    assert remaining.accumulated_cents == initial.accumulated_cents


def test_independent_connections_cannot_both_spend_on_old_yellow(
    db_path: Path, registry: ToolRegistry, gateway: FactGateway, tmp_path: Path,
) -> None:
    barrier = Barrier(2)

    def run(cents: int, label: str) -> str:
        writer = Store(db_path, StoreMode.READ_WRITE)
        reader = Store(db_path, StoreMode.READ_ONLY)
        try:
            executor = Pep(
                registry, FactGateway(reader), CoreBank(writer),
                AuditLog(tmp_path / f"{label}.jsonl"), CircuitBreaker(),
                WRITE_HANDLERS, QUERY_HANDLERS,
            )
            call = _call(cents)
            barrier.wait(timeout=5)
            try:
                _run(executor, call, Level.YELLOW, label)
                return "committed"
            except WritePreconditionError as error:
                assert error.result.status is PreconditionStatus.REAUTHORIZE
                return "reauthorize"
        finally:
            reader.close()
            writer.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, 60_000, "concurrent-a")
        second = pool.submit(run, 70_000, "concurrent-b")
        assert sorted((first.result(), second.result())) == ["committed", "reauthorize"]
    assert gateway.daily_transfer_total(DEMO_USER_ID, DEFAULT_BOOKING_DATE).cents in (
        60_000, 70_000
    )


def test_post_commit_failure_carries_replayable_receipt(
    pep: Pep, gateway: FactGateway, monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = _call(20_000)
    before = gateway.daily_transfer_total(DEMO_USER_ID, DEFAULT_BOOKING_DATE).cents

    def fail_verify(_receipt: object) -> tuple[tuple[str, str], ...]:
        raise GatewayError("injected verification failure")

    monkeypatch.setattr(pep, "_verify_receipt", fail_verify)
    with pytest.raises(CommittedWriteError) as error:
        _run(pep, call, Level.YELLOW, "post-commit")
    assert error.value.receipt.idempotency_key == "post-commit"
    assert gateway.daily_transfer_total(DEMO_USER_ID, DEFAULT_BOOKING_DATE).cents == before + 20_000
    with pytest.raises(CommittedWriteError) as replay:
        _run(pep, call, Level.YELLOW, "post-commit")
    assert replay.value.receipt.replayed is True
    assert gateway.daily_transfer_total(DEMO_USER_ID, DEFAULT_BOOKING_DATE).cents == before + 20_000


def test_post_commit_replay_survives_payee_becoming_blocked(
    pep: Pep, gateway: FactGateway, rw_store: Store, monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = _call(20_000)
    plan = manifest_plan(call)
    before = gateway.account("acc_savings").payload.balance.cents

    def fail_verify(_receipt: object) -> tuple[tuple[str, str], ...]:
        raise GatewayError("injected verification failure")

    with monkeypatch.context() as patch:
        patch.setattr(pep, "_verify_receipt", fail_verify)
        with pytest.raises(CommittedWriteError) as error:
            pep.execute(
                call, _decision(Level.YELLOW), plan, "committed-blocked",
                execution=_grant(pep, call, Level.YELLOW, plan=plan),
            )
    with rw_store.transaction() as conn:
        conn.execute("UPDATE payees SET blocked = 1 WHERE payee_id = 'p_lina'")
    replay = pep.execute(
        call, _decision(Level.YELLOW), plan, "committed-blocked",
        execution=_grant(pep, call, Level.YELLOW, plan=plan),
    )
    assert replay.record_ids == error.value.receipt.record_ids
    assert "幂等回放" in replay.message
    assert gateway.account("acc_savings").payload.balance.cents == before - 20_000
    assert gateway.daily_transfer_total(DEMO_USER_ID, DEFAULT_BOOKING_DATE).cents == 20_000


def test_transaction_reads_authoritative_global_breaker(
    pep: Pep, gateway: FactGateway, rw_store: Store,
) -> None:
    init_breaker_schema(rw_store)
    with rw_store.transaction() as conn:
        conn.execute(
            "INSERT INTO breaker_state(breaker_id, state, failures, triggers)"
            " VALUES('default', 'open', 0, '')"
        )
    call = _call(20_000)
    before = gateway.daily_transfer_total(DEMO_USER_ID, DEFAULT_BOOKING_DATE).cents
    with pytest.raises(WritePreconditionError) as error:
        _run(pep, call, Level.YELLOW, "breaker-open")
    assert error.value.result.status is PreconditionStatus.TAKEOVER
    assert error.value.result.reasons
    assert gateway.daily_transfer_total(DEMO_USER_ID, DEFAULT_BOOKING_DATE).cents == before
