from datetime import date
from pathlib import Path

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.plan import Plan
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.gateway.transfer_store import init_transfer_schema
from ai_banking_copilot.policy.audit import AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.pep import Pep
from ai_banking_copilot.scenarios.registry import QUERY_HANDLERS, WRITE_HANDLERS
from ai_banking_copilot.scenarios.transfer import due_transfer_plan
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.tools.registry import ToolCall


def _setup(tmp_path: Path) -> tuple[Store, Store, FactGateway, str]:
    writer = Store(tmp_path / "due.db")
    init_schema(writer)
    seed_database(writer)
    init_transfer_schema(writer)
    receipt = CoreBank(writer).book_transfer(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        payee_id="p_lina",
        amount=Money(10000),
        booked_on=date(2026, 10, 1),
        remark="",
        idempotency_key="due-booking",
    )
    reader = Store(writer.path, StoreMode.READ_ONLY)
    return writer, reader, FactGateway(reader), receipt.record_ids[0].split(":", 1)[1]


def test_due_plan_filters_owner_status_and_binds_persistent_identity(tmp_path: Path) -> None:
    writer, reader, gateway, schedule_id = _setup(tmp_path)
    try:
        with writer.transaction() as conn:
            conn.execute("INSERT INTO users VALUES('u_other', '另一用户', 'R2')")
            conn.execute(
                "INSERT INTO scheduled_transfers VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                ("other", "u_other", "acc_savings", "p_lina", 10000, "2026-10-01", "", "scheduled"),
            )
            conn.execute(
                "INSERT INTO scheduled_transfers VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "done", DEMO_USER_ID, "acc_savings", "p_lina", 10000,
                    "2026-10-01", "", "completed",
                ),
            )
        nodes = due_transfer_plan(gateway, date(2026, 10, 1))
        assert len(nodes) == 1
        assert nodes[0].params["schedule_id"] == schedule_id
        assert nodes[0].params["executed_on"] == "2026-10-01"
        assert due_transfer_plan(gateway, date(2026, 9, 30)) == ()
    finally:
        reader.close()
        writer.close()


def test_due_execution_and_replay_pass_through_pep_with_provenance(tmp_path: Path) -> None:
    writer, reader, gateway, schedule_id = _setup(tmp_path)
    try:
        node = due_transfer_plan(gateway, date(2026, 10, 1))[0]
        params: dict[str, object] = {**node.params, "user_id": DEMO_USER_ID}
        for name in ("booked_on", "executed_on"):
            params[name] = date.fromisoformat(str(params[name]))
        call = ToolCall(
            action=node.action,
            params={key: Value(value, Trust.TRUSTED) for key, value in params.items()},
        )
        audit = AuditLog(tmp_path / "audit.jsonl")
        pep = Pep(
            build_default_registry(), gateway, CoreBank(writer),
            audit, CircuitBreaker(), WRITE_HANDLERS, QUERY_HANDLERS,
        )
        decision = Decision(level=Level.YELLOW, reasons=(ReasonCode.STATE_CHANGE_SMALL,))
        first_plan = Plan(plan_id="first", nodes=(node,))
        second_plan = Plan(plan_id="second", nodes=(node,))
        first = pep.execute(
            call, decision, first_plan, "first-key",
            execution=pep.issue_write_grant(
                call, decision, first_plan, approved_level=Level.YELLOW,
            ),
        )
        second = pep.execute(
            call, decision, second_plan, "second-key",
            execution=pep.issue_write_grant(
                call, decision, second_plan, approved_level=Level.YELLOW,
            ),
        )
        assert first.status == second.status == "settled"
        assert first.record_ids == second.record_ids
        assert len(audit.events()[0].payload["provenance"]) == 2
        assert "幂等回放" in second.message
        assert gateway.scheduled_transfer(schedule_id).payload.status == "completed"
        assert due_transfer_plan(gateway, date(2026, 10, 1)) == ()
    finally:
        reader.close()
        writer.close()
