import pytest

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.contracts.plan import cfhash
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.plan_card import PlanCard
from ai_banking_copilot.web.session import (
    ConfirmationMismatchError,
    Session,
    SessionState,
    SessionStateError,
    StrongVerificationRequiredError,
)


def _awaiting(deps: AppDeps) -> tuple[Session, PlanCard]:
    session = Session(deps)
    view = session.submit("转给李娜 500 元")
    assert view.card is not None
    return session, view.card


def test_confirm_executes_and_debits(deps: AppDeps, rw_store: Store) -> None:
    session, card = _awaiting(deps)
    view = session.confirm(card.card_id, frozen_hash=card.frozen_hash)
    assert view.state is SessionState.COMPLETED
    assert view.record_ids and view.record_ids[0].startswith("transactions:")
    rows = rw_store.query("SELECT balance_cents FROM accounts WHERE account_id = 'acc_savings'")
    assert rows[0]["balance_cents"] == 12843000 - 50000
    assert deps.audit.verify_chain() is True


def test_confirm_binds_frozen_plan_and_hash(deps: AppDeps) -> None:
    session, card = _awaiting(deps)
    session.confirm(card.card_id, frozen_hash=card.frozen_hash)
    record = session.confirmation()
    assert record is not None
    assert record.frozen_hash == card.frozen_hash
    assert cfhash(record.plan.nodes) == card.frozen_hash


def test_confirm_rejects_a_tampered_hash(deps: AppDeps) -> None:
    session, card = _awaiting(deps)
    with pytest.raises(StalePlanError, match="哈希不匹配"):
        session.confirm(card.card_id, frozen_hash="0" * 64)


def test_confirm_rejects_a_wrong_card_id(deps: AppDeps) -> None:
    session, card = _awaiting(deps)
    with pytest.raises(ConfirmationMismatchError):
        session.confirm("card_nope", frozen_hash=card.frozen_hash)


def test_confirm_rejects_when_nothing_submitted(deps: AppDeps) -> None:
    with pytest.raises(SessionStateError):
        Session(deps).confirm("card_x", frozen_hash="0" * 64)


def test_over_limit_requires_strong_verification(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("转给李娜 1500 元")
    assert view.card is not None
    assert view.card.level is Level.RED
    with pytest.raises(StrongVerificationRequiredError):
        session.confirm(view.card.card_id, frozen_hash=view.card.frozen_hash)
    done = session.confirm(
        view.card.card_id, frozen_hash=view.card.frozen_hash, strong_verified=True
    )
    assert done.state is SessionState.COMPLETED


def test_open_breaker_routes_a_write_to_takeover(deps: AppDeps) -> None:
    session, card = _awaiting(deps)
    deps.breaker.lock()
    view = session.confirm(card.card_id, frozen_hash=card.frozen_hash)
    assert view.state is SessionState.TAKEN_OVER
    assert view.exec_results == ()


def test_open_breaker_does_not_block_a_pure_query(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("查一下上月账单")
    assert view.state is SessionState.AWAITING_CONFIRM
    assert view.card is not None
    assert view.card.level is Level.GREEN
    deps.breaker.lock()
    done = session.confirm(view.card.card_id, frozen_hash=view.card.frozen_hash)
    # 03 已按裁决 #14 覆盖全部纯查询动作（含 bills.category_trend）：熔断打开不影响纯查询，
    # 两段纯查询 DAG 应全量执行完成。
    assert done.state is SessionState.COMPLETED
    assert len(done.exec_results) == 2


def test_idempotent_replay_does_not_double_write(deps: AppDeps, rw_store: Store) -> None:
    session, card = _awaiting(deps)
    session.confirm(card.card_id, frozen_hash=card.frozen_hash)
    rows = rw_store.query(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = '转账' AND channel = 'agent'"
    )
    assert rows[0]["n"] == 1
