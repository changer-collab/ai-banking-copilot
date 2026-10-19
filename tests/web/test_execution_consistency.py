"""确认、执行身份与续执行的业务回归。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.web.app import AppDeps, create_app
from ai_banking_copilot.web.session import (
    ConfirmationMismatchError,
    Session,
    SessionState,
    StrongVerificationRequiredError,
)


def _transfers(store: Store) -> int:
    return int(store.query(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = '转账' AND channel = 'agent'"
    )[0]["n"])


def test_independent_identical_submissions_make_two_transfers(
    deps: AppDeps, rw_store: Store,
) -> None:
    session = Session(deps)
    executions: list[str] = []
    for _ in range(2):
        submitted = session.submit("转给李娜200元")
        assert submitted.card is not None
        done = session.confirm(submitted.card.card_id, frozen_hash=submitted.card.frozen_hash)
        assert done.state is SessionState.COMPLETED
        confirmation = session.confirmation()
        assert confirmation is not None
        executions.append(confirmation.execution_id)
    assert len(set(executions)) == 2
    assert _transfers(rw_store) == 2


@pytest.mark.parametrize("amount", [200, 1500])
def test_resume_before_confirmation_never_writes(
    deps: AppDeps, rw_store: Store, amount: int,
) -> None:
    session = Session(deps)
    session.submit(f"转给李娜{amount}元")
    session.interrupt()
    resumed = session.resume()
    assert resumed.state is SessionState.AWAITING_CONFIRM
    assert session.confirmation() is None
    assert _transfers(rw_store) == 0


def test_stale_yellow_confirmation_refreshes_red_card(
    deps: AppDeps, rw_store: Store,
) -> None:
    first, second = Session(deps), Session(deps)
    first_card = first.submit("转给李娜600元").card
    second_card = second.submit("转给李娜700元").card
    assert first_card is not None and second_card is not None
    assert first_card.level is second_card.level is Level.YELLOW
    assert first.confirm(first_card.card_id, frozen_hash=first_card.frozen_hash).state \
        is SessionState.COMPLETED
    stopped = second.confirm(second_card.card_id, frozen_hash=second_card.frozen_hash)
    assert stopped.state is SessionState.AWAITING_CONFIRM
    refreshed = second.card()
    assert refreshed is not None and refreshed.level is Level.RED
    assert stopped.card == refreshed
    assert stopped.quota is not None and stopped.quota.accumulated_cents == 130000
    assert _transfers(rw_store) == 1
    with pytest.raises(StrongVerificationRequiredError):
        second.confirm(refreshed.card_id, frozen_hash=refreshed.frozen_hash)
    code = second.request_verification().code
    done = second.confirm(
        refreshed.card_id, frozen_hash=refreshed.frozen_hash, verification_code=code,
    )
    assert done.state is SessionState.COMPLETED
    assert _transfers(rw_store) == 2


def test_confirm_retry_reuses_execution_and_receipt(deps: AppDeps, rw_store: Store) -> None:
    session = Session(deps)
    card = session.submit("转给李娜200元").card
    assert card is not None
    first = session.confirm(card.card_id, frozen_hash=card.frozen_hash)
    second = session.confirm(card.card_id, frozen_hash=card.frozen_hash)
    assert second.state is SessionState.COMPLETED
    assert second.record_ids == first.record_ids
    assert _transfers(rw_store) == 1


def test_verified_resume_keeps_original_grant(deps: AppDeps, rw_store: Store) -> None:
    session = Session(deps)
    card = session.submit("冻结1100元，然后转给李娜300元").card
    assert card is not None and card.level is Level.RED
    prompt = session.request_verification()
    part = session.confirm(
        card.card_id, frozen_hash=card.frozen_hash, verification_code=prompt.code, steps=1,
    )
    assert part.state is SessionState.EXECUTING
    session.interrupt()
    assert session.resume().state is SessionState.COMPLETED
    assert _transfers(rw_store) == 1


def test_resume_rechecks_global_breaker(deps: AppDeps, rw_store: Store) -> None:
    session = Session(deps)
    card = session.submit("冻结500元，然后转给李娜300元").card
    assert card is not None
    session.confirm(card.card_id, frozen_hash=card.frozen_hash, steps=1)
    session.interrupt()
    deps.breaker.lock()
    assert session.resume().state is SessionState.TAKEN_OVER
    assert _transfers(rw_store) == 0


def test_old_card_cannot_confirm_identical_new_submission(
    deps: AppDeps, rw_store: Store,
) -> None:
    session = Session(deps)
    old = session.submit("转给李娜200元").card
    current = session.submit("转给李娜200元").card
    assert old is not None and current is not None
    assert old.frozen_hash == current.frozen_hash
    with pytest.raises(ConfirmationMismatchError):
        session.confirm(old.card_id, frozen_hash=old.frozen_hash)
    assert _transfers(rw_store) == 0


def test_old_challenge_cannot_authorize_identical_new_submission(
    deps: AppDeps, rw_store: Store,
) -> None:
    session = Session(deps)
    session.submit("转给李娜1500元")
    old_code = session.request_verification().code
    card = session.submit("转给李娜1500元").card
    assert card is not None
    with pytest.raises(StrongVerificationRequiredError):
        session.confirm(card.card_id, frozen_hash=card.frozen_hash, verification_code=old_code)
    assert _transfers(rw_store) == 0


def test_clarification_keeps_identity_until_a_plan_exists(deps: AppDeps) -> None:
    session = Session(deps)
    missing = session.submit("转给李娜", selected={"account": "acc_savings"})
    assert missing.state is SessionState.NEEDS_CLARIFICATION
    execution_id = session._execution_id
    clarified = session.clarify({"amount": 20000})
    assert clarified.card is not None
    assert session._execution_id == execution_id
    changed = session.clarify({"amount": 30000})
    assert changed.card is not None
    assert session._execution_id != execution_id


def test_http_confirmation_returns_refreshed_card_and_quota(
    deps: AppDeps, rw_store: Store,
) -> None:
    client = TestClient(create_app(deps))
    first = client.post("/api/submit", json={
        "session_id": "first", "utterance": "转给李娜600元",
    }).json()["card"]
    second = client.post("/api/submit", json={
        "session_id": "second", "utterance": "转给李娜700元",
    }).json()["card"]
    client.post("/api/confirm", json={
        "session_id": "first", "card_id": first["card_id"],
        "frozen_hash": first["frozen_hash"],
    }).raise_for_status()
    stopped = client.post("/api/confirm", json={
        "session_id": "second", "card_id": second["card_id"],
        "frozen_hash": second["frozen_hash"],
    })
    stopped.raise_for_status()
    body = stopped.json()
    assert body["state"] == "awaiting_confirm"
    assert body["card"]["level"] == "RED"
    assert body["card"]["daily_accumulated_cents"] == 130000
    assert "strong_verify" in body["card"]["requires"]
    assert body["decision"]["level"] == "RED"
    assert "AMOUNT_OVER_DAILY_LIMIT" in body["decision"]["reasons"]
    assert _transfers(rw_store) == 1


def test_future_booking_uses_its_actual_day_for_risk(
    deps: AppDeps, rw_store: Store,
) -> None:
    first, second = Session(deps), Session(deps)
    first_card = first.submit("明天转给李娜600元").card
    second_card = second.submit("明天转给李娜600元").card
    assert first_card is not None and second_card is not None
    assert first_card.level is second_card.level is Level.YELLOW
    assert first.confirm(first_card.card_id, frozen_hash=first_card.frozen_hash).state \
        is SessionState.COMPLETED
    stopped = second.confirm(second_card.card_id, frozen_hash=second_card.frozen_hash)
    assert stopped.state is SessionState.AWAITING_CONFIRM
    assert stopped.card is not None and stopped.card.level is Level.RED
    assert stopped.quota is not None and stopped.quota.accumulated_cents == 120000
    assert _transfers(rw_store) == 1
