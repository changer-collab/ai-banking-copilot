"""会话执行并发、提交后失败与续执行回归。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.gateway.core_bank import WriteReceipt
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.policy.audit import KIND_EXECUTION, AuditEvent
from ai_banking_copilot.policy.saga import ReversalState
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import ExecutionView, Session, SessionState, SessionStateError


def _transfers(store: Store) -> int:
    return int(
        store.query(
            "SELECT COUNT(*) AS n FROM transactions "
            "WHERE category = '转账' AND channel = 'agent'"
        )[0]["n"]
    )


def test_same_session_concurrent_confirm_is_idempotent(
    deps: AppDeps, rw_store: Store
) -> None:
    session = Session(deps)
    view = session.submit("转给李娜 200 元")
    assert view.card is not None
    card = view.card
    before = deps.gateway.account("acc_savings").payload.balance.cents
    start = Barrier(2)

    def confirm() -> ExecutionView:
        start.wait(timeout=5)
        return session.confirm(card.card_id, frozen_hash=card.frozen_hash)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(confirm), pool.submit(confirm))]

    assert all(result.state is SessionState.COMPLETED for result in results)
    assert _transfers(rw_store) == 1
    assert results[0].record_ids == results[1].record_ids
    assert len(results[0].record_ids) == 1
    assert deps.gateway.account("acc_savings").payload.balance.cents == before - 20_000


@pytest.mark.parametrize("failure", ["verification", "execution_audit"])
def test_post_commit_failure_reports_receipt_and_resume_reuses_original_key(
    deps: AppDeps, rw_store: Store, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    session = Session(deps)
    view = session.submit("转给李娜 600 元，然后转给李娜 300 元")
    assert view.card is not None and view.plan is not None
    assert len(view.plan.nodes) == 2
    assert view.card.level is Level.YELLOW
    card = view.card
    before = deps.gateway.account("acc_savings").payload.balance.cents
    original_verify = deps.pep._verify_receipt
    original_append = deps.audit.append
    failed = True
    receipts: list[WriteReceipt] = []

    def verify(receipt: WriteReceipt) -> tuple[tuple[str, str], ...]:
        nonlocal failed
        receipts.append(receipt)
        if failed and failure == "verification":
            failed = False
            raise RuntimeError("injected receipt verification failure")
        return original_verify(receipt)

    def append(event: AuditEvent) -> str:
        nonlocal failed
        if failed and failure == "execution_audit" and event.kind == KIND_EXECUTION:
            failed = False
            raise RuntimeError("injected execution audit failure")
        return original_append(event)

    monkeypatch.setattr(deps.pep, "_verify_receipt", verify)
    monkeypatch.setattr(deps.audit, "append", append)
    first = session.confirm(card.card_id, frozen_hash=card.frozen_hash)
    assert first.state is SessionState.INTERRUPTED
    assert "已提交" in first.message
    assert len(first.exec_results) == 1
    assert first.exec_results[0].status == "committed_unverified"
    assert first.record_ids == receipts[0].record_ids
    assert _transfers(rw_store) == 1
    assert deps.gateway.account("acc_savings").payload.balance.cents == before - 60_000
    stored_keys = rw_store.query("SELECT idempotency_key FROM write_receipts")
    assert [row["idempotency_key"] for row in stored_keys] == [receipts[0].idempotency_key]

    resumed = session.resume()
    assert resumed.state is SessionState.COMPLETED
    assert len(resumed.exec_results) == 2
    assert all(result.status != "committed_unverified" for result in resumed.exec_results)
    assert first.record_ids[0] in resumed.record_ids
    assert len(set(resumed.record_ids)) == 2
    assert _transfers(rw_store) == 2
    assert deps.gateway.account("acc_savings").payload.balance.cents == before - 90_000
    assert receipts[0].idempotency_key == receipts[1].idempotency_key
    assert receipts[1].replayed
    assert not receipts[2].replayed
    assert len(rw_store.query("SELECT idempotency_key FROM write_receipts")) == 2


def test_reauthorization_does_not_prevent_reversing_an_already_completed_transfer(
    deps: AppDeps, rw_store: Store
) -> None:
    before = deps.gateway.account("acc_savings").payload.balance.cents
    session = Session(deps)
    view = session.submit("转给李娜 600 元，然后转给李娜 300 元")
    assert view.card is not None and view.card.level is Level.YELLOW
    first = session.confirm(view.card.card_id, frozen_hash=view.card.frozen_hash, steps=1)
    assert first.state is SessionState.EXECUTING
    assert _transfers(rw_store) == 1
    session.interrupt()

    other = Session(deps)
    other_view = other.submit("转给李娜 200 元")
    assert other_view.card is not None
    assert other.confirm(
        other_view.card.card_id, frozen_hash=other_view.card.frozen_hash
    ).state is SessionState.COMPLETED
    resumed = session.resume()
    assert resumed.state is SessionState.AWAITING_CONFIRM
    assert resumed.card is not None and resumed.card.level is Level.RED
    assert session.confirmation() is None
    assert _transfers(rw_store) == 2
    assert deps.gateway.account("acc_savings").payload.balance.cents == before - 80_000

    outcome = session.reverse()
    assert outcome.state is ReversalState.REVERSED
    assert "原扣款已回补" in outcome.message
    assert len(outcome.compensated_steps) == 1
    assert _transfers(rw_store) == 2
    assert deps.gateway.account("acc_savings").payload.balance.cents == before - 20_000
    reversals = rw_store.query("SELECT amount_cents FROM transactions WHERE category = '冲正'")
    assert [row["amount_cents"] for row in reversals] == [60_000]


def test_reverse_requires_recovery_of_a_committed_unverified_receipt(
    deps: AppDeps, rw_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = deps.gateway.account("acc_savings").payload.balance.cents
    session = Session(deps)
    view = session.submit("转给李娜 600 元，然后转给李娜 300 元")
    assert view.card is not None

    def fail_verification(_receipt: WriteReceipt) -> tuple[tuple[str, str], ...]:
        raise RuntimeError("injected receipt verification failure")

    monkeypatch.setattr(deps.pep, "_verify_receipt", fail_verification)
    interrupted = session.confirm(view.card.card_id, frozen_hash=view.card.frozen_hash)
    assert interrupted.state is SessionState.INTERRUPTED
    assert interrupted.exec_results[0].status == "committed_unverified"
    assert "已提交" in interrupted.message
    with pytest.raises(SessionStateError, match="已提交.*先恢复"):
        session.reverse()
    assert session.state() is SessionState.INTERRUPTED
    assert _transfers(rw_store) == 1
    assert deps.gateway.account("acc_savings").payload.balance.cents == before - 60_000
    reversals = rw_store.query("SELECT COUNT(*) AS n FROM transactions WHERE category = '冲正'")
    assert reversals[0]["n"] == 0


@pytest.mark.parametrize("external_spend", [False, True])
def test_resume_does_not_double_count_completed_transfer_and_rechecks_external_spend(
    deps: AppDeps, rw_store: Store, external_spend: bool
) -> None:
    session = Session(deps)
    view = session.submit("转给李娜 600 元，然后转给李娜 300 元")
    assert view.card is not None and view.card.level is Level.YELLOW
    first = session.confirm(view.card.card_id, frozen_hash=view.card.frozen_hash, steps=1)
    assert first.state is SessionState.EXECUTING
    session.interrupt()
    assert _transfers(rw_store) == 1

    if external_spend:
        other = Session(deps)
        other_view = other.submit("转给李娜 200 元")
        assert other_view.card is not None
        assert other.confirm(
            other_view.card.card_id, frozen_hash=other_view.card.frozen_hash
        ).state is SessionState.COMPLETED

    resumed = session.resume()
    if external_spend:
        assert resumed.state is SessionState.AWAITING_CONFIRM
        assert resumed.card is not None and resumed.card.level is Level.RED
        assert resumed.quota is not None and resumed.quota.accumulated_cents == 110_000
        assert session.confirmation() is None
        assert len(resumed.exec_results) == 1
    else:
        assert resumed.state is SessionState.COMPLETED
        assert resumed.card is not None and resumed.card.level is Level.YELLOW
        assert len(resumed.exec_results) == 2
    assert _transfers(rw_store) == 2
