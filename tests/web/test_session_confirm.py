from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.contracts.plan import cfhash
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.policy.audit import (
    KIND_BREAKER,
    KIND_DECISION,
    KIND_EXECUTION,
    AuditEvent,
)
from ai_banking_copilot.policy.breaker import BreakerTrigger, CircuitBreaker
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.policy.verification import (
    ChallengeStatus,
    DeliveryResult,
    HmacStrongVerificationProofVerifier,
    VerificationAttemptLimiter,
    VerificationBinding,
)
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.plan_card import PlanCard
from ai_banking_copilot.web.session import (
    MAX_CONFIRM_STEPS,
    PLAN_TTL_S,
    VERIFICATION_TTL_S,
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


def test_confirmation_spoof_is_audited_and_opens_global_breaker(
    deps: AppDeps, rw_store: Store
) -> None:
    """伪造卡片 / 哈希均拒绝且计入全局连续授权失败。"""
    session, card = _awaiting(deps)
    attempts = (
        ("card-nope", card.frozen_hash, ConfirmationMismatchError),
        (card.card_id, "0" * 64, StalePlanError),
        ("card-nope-2", card.frozen_hash, ConfirmationMismatchError),
    )
    for supplied_card, supplied_hash, error_type in attempts:
        with pytest.raises(error_type):
            session.confirm(supplied_card, frozen_hash=supplied_hash)

    assert deps.breaker.is_open() is True
    assert BreakerTrigger.CONSECUTIVE_AUTH_FAILURES in deps.breaker.triggers()
    failed = [
        event for event in deps.audit.events()
        if event.payload.get("kind") == "confirmation"
        and event.payload.get("status") == "failed"
    ]
    assert [event.payload["reason"] for event in failed[-3:]] == [
        "card_mismatch", "hash_mismatch", "card_mismatch"
    ]
    for event in failed[-3:]:
        assert event.payload["user_id"] == deps.user_id
        assert event.payload["session_id"] == session._session_id
        assert event.payload["execution_id"] == session._execution_id
        assert event.payload["card_id"] == card.card_id
        assert event.payload["frozen_hash"] == card.frozen_hash
    rows = rw_store.query(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = '转账' AND channel = 'agent'"
    )
    assert rows[0]["n"] == 0


def test_completed_confirmation_replay_keeps_idempotency_but_rejects_spoof(
    deps: AppDeps, rw_store: Store
) -> None:
    session, card = _awaiting(deps)
    first = session.confirm(card.card_id, frozen_hash=card.frozen_hash)
    failures = deps.breaker.failure_count()

    with pytest.raises(StalePlanError, match="哈希不匹配"):
        session.confirm(card.card_id, frozen_hash="0" * 64)
    assert deps.breaker.failure_count() == failures + 1

    replay = session.confirm(card.card_id, frozen_hash=card.frozen_hash)
    assert replay.state is SessionState.COMPLETED
    assert deps.breaker.failure_count() == failures + 1
    rows = rw_store.query(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = '转账' AND channel = 'agent'"
    )
    assert rows[0]["n"] == len(first.record_ids)


def test_confirm_rejects_when_nothing_submitted(deps: AppDeps) -> None:
    with pytest.raises(SessionStateError):
        Session(deps).confirm("card_x", frozen_hash="0" * 64)


def _red_card(deps: AppDeps) -> tuple[Session, PlanCard]:
    session = Session(deps)
    view = session.submit("转给李娜 1500 元")
    assert view.card is not None
    assert view.card.level is Level.RED
    return session, view.card


def test_over_limit_requires_strong_verification(deps: AppDeps) -> None:
    session, card = _red_card(deps)
    with pytest.raises(StrongVerificationRequiredError):
        session.confirm(card.card_id, frozen_hash=card.frozen_hash)
    prompt = session.request_verification()
    done = session.confirm(
        card.card_id, frozen_hash=card.frozen_hash, verification_code=prompt.code
    )
    assert done.state is SessionState.COMPLETED


@pytest.mark.parametrize("stale_proof", [False, True])
def test_failed_consumption_audit_cannot_reuse_the_challenge(
    deps: AppDeps, rw_store: Store, monkeypatch: pytest.MonkeyPatch, stale_proof: bool
) -> None:
    session, card = _red_card(deps)
    prompt = session.request_verification()
    if stale_proof:
        challenge = session._challenge
        assert challenge is not None
        session._strong_verification_proof = HmacStrongVerificationProofVerifier(
            session._verification_key
        ).issue(replace(
            challenge, status=ChallengeStatus.CONSUMED,
            binding=replace(challenge.binding, execution_id="prior-execution"),
        ))
    original_append = deps.audit.append
    failed = True

    def append(event: AuditEvent) -> str:
        nonlocal failed
        payload = event.payload
        if (
            failed
            and event.kind == KIND_DECISION
            and payload.get("kind") == "verification"
            and payload.get("status") == "consumed"
        ):
            failed = False
            raise RuntimeError("injected consumed audit failure")
        return original_append(event)

    monkeypatch.setattr(deps.audit, "append", append)
    with pytest.raises(RuntimeError, match="consumed audit failure"):
        session.confirm(
            card.card_id, frozen_hash=card.frozen_hash, verification_code=prompt.code
        )
    assert session._challenge is not None
    assert session._challenge.status is ChallengeStatus.CONSUMED
    assert session._strong_verification_proof is None
    assert session.confirmation() is None

    with pytest.raises(StrongVerificationRequiredError, match="已失效"):
        session.confirm(
            card.card_id, frozen_hash=card.frozen_hash, verification_code=prompt.code
        )

    rows = rw_store.query(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = '转账' AND channel = 'agent'"
    )
    assert rows[0]["n"] == 0

    fresh = session.request_verification()
    done = session.confirm(
        card.card_id, frozen_hash=card.frozen_hash, verification_code=fresh.code
    )
    assert done.state is SessionState.COMPLETED
    rows = rw_store.query(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = '转账' AND channel = 'agent'"
    )
    assert rows[0]["n"] == 1
    assert deps.audit.verify_chain() is True


def test_delivery_audit_failure_invalidates_the_undisclosed_challenge(
    deps: AppDeps, rw_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, card = _red_card(deps)
    provider = session._verification_provider
    original_deliver = provider.deliver
    delivered_codes: list[str] = []

    def deliver(
        binding: VerificationBinding,
        challenge_id: str,
        code: str,
        expires_at: datetime,
    ) -> DeliveryResult:
        result = original_deliver(binding, challenge_id, code, expires_at)
        delivered_codes.append(code)
        return result

    original_append = deps.audit.append
    failed = True

    def append(event: AuditEvent) -> str:
        nonlocal failed
        if (
            failed
            and event.kind == KIND_DECISION
            and event.payload.get("kind") == "verification"
            and event.payload.get("status") == "delivered"
        ):
            failed = False
            raise RuntimeError("injected delivery audit failure")
        return original_append(event)

    monkeypatch.setattr(provider, "deliver", deliver)
    monkeypatch.setattr(deps.audit, "append", append)
    with pytest.raises(RuntimeError, match="delivery audit failure"):
        session.request_verification()
    assert delivered_codes
    assert session._challenge is not None
    assert session._challenge.status is ChallengeStatus.REPLACED
    assert session._strong_verification_proof is None
    with pytest.raises(StrongVerificationRequiredError, match="已失效"):
        session.confirm(
            card.card_id,
            frozen_hash=card.frozen_hash,
            verification_code=delivered_codes[0],
        )
    rows = rw_store.query(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = '转账' AND channel = 'agent'"
    )
    assert rows[0]["n"] == 0

    fresh = session.request_verification()
    done = session.confirm(
        card.card_id, frozen_hash=card.frozen_hash, verification_code=fresh.code
    )
    assert done.state is SessionState.COMPLETED
    assert deps.audit.verify_chain() is True


def test_confirmation_audit_failure_discards_the_cached_proof(
    deps: AppDeps, rw_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, card = _red_card(deps)
    prompt = session.request_verification()
    original_append = deps.audit.append
    failed = True

    def append(event: AuditEvent) -> str:
        nonlocal failed
        if (
            failed
            and event.kind == KIND_DECISION
            and event.payload.get("kind") == "confirmation"
        ):
            failed = False
            raise RuntimeError("injected confirmation audit failure")
        return original_append(event)

    monkeypatch.setattr(deps.audit, "append", append)
    with pytest.raises(RuntimeError, match="confirmation audit failure"):
        session.confirm(
            card.card_id, frozen_hash=card.frozen_hash, verification_code=prompt.code
        )
    assert session._challenge is not None
    assert session._challenge.status is ChallengeStatus.CONSUMED
    assert session._strong_verification_proof is None
    assert session.confirmation() is None
    rows = rw_store.query(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = '转账' AND channel = 'agent'"
    )
    assert rows[0]["n"] == 0
    with pytest.raises(StrongVerificationRequiredError, match="已失效"):
        session.confirm(
            card.card_id, frozen_hash=card.frozen_hash, verification_code=prompt.code
        )

    fresh = session.request_verification()
    done = session.confirm(
        card.card_id, frozen_hash=card.frozen_hash, verification_code=fresh.code
    )
    assert done.state is SessionState.COMPLETED
    assert deps.audit.verify_chain() is True


def test_limiter_reset_failure_keeps_challenge_consumed_without_a_proof(
    deps: AppDeps, rw_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, card = _red_card(deps)
    prompt = session.request_verification()
    original_reset = deps.verification_limiter.reset

    def fail_reset(user_id: str) -> None:
        raise RuntimeError("injected limiter reset failure")

    monkeypatch.setattr(deps.verification_limiter, "reset", fail_reset)
    with pytest.raises(RuntimeError, match="limiter reset failure"):
        session.confirm(
            card.card_id, frozen_hash=card.frozen_hash, verification_code=prompt.code
        )
    assert session._challenge is not None
    assert session._challenge.status is ChallengeStatus.CONSUMED
    assert session._strong_verification_proof is None
    assert session.confirmation() is None
    with pytest.raises(StrongVerificationRequiredError, match="已失效"):
        session.confirm(
            card.card_id, frozen_hash=card.frozen_hash, verification_code=prompt.code
        )
    rows = rw_store.query(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = '转账' AND channel = 'agent'"
    )
    assert rows[0]["n"] == 0

    monkeypatch.setattr(deps.verification_limiter, "reset", original_reset)
    fresh = session.request_verification()
    done = session.confirm(
        card.card_id, frozen_hash=card.frozen_hash, verification_code=fresh.code
    )
    assert done.state is SessionState.COMPLETED
    assert deps.audit.verify_chain() is True


def test_pdp_decision_and_confirmation_audits_share_utc_identity(deps: AppDeps) -> None:
    session = Session(deps, session_id="audit-session")
    view = session.submit("转给李娜 500 元")
    assert view.card is not None
    decision = next(
        event for event in deps.audit.events()
        if event.payload.get("kind") == "authorization_decision"
    )
    done = session.confirm(view.card.card_id, frozen_hash=view.card.frozen_hash)
    assert done.state is SessionState.COMPLETED
    confirmation = next(
        event for event in deps.audit.events()
        if event.payload.get("kind") == "confirmation"
    )

    identity_keys = (
        "user_id", "session_id", "execution_id", "card_id", "frozen_hash"
    )
    assert {key: decision.payload[key] for key in identity_keys} == {
        "user_id": deps.user_id,
        "session_id": "audit-session",
        "execution_id": confirmation.payload["execution_id"],
        "card_id": view.card.card_id,
        "frozen_hash": view.card.frozen_hash,
    }
    assert {key: confirmation.payload[key] for key in identity_keys} == {
        key: decision.payload[key] for key in identity_keys
    }
    for event in (decision, confirmation):
        timestamp = datetime.fromisoformat(event.timestamp.replace("Z", "+00:00"))
        assert timestamp.tzinfo is not None and timestamp.utcoffset() == timedelta(0)


def test_confirmed_query_execution_audit_keeps_session_identity(deps: AppDeps) -> None:
    session = Session(deps, session_id="query-audit-session")
    view = session.submit("查看账单，再查看分类趋势")
    assert view.card is not None
    done = session.confirm(view.card.card_id, frozen_hash=view.card.frozen_hash)
    assert done.state is SessionState.COMPLETED
    confirmation = next(
        event for event in deps.audit.events()
        if event.payload.get("kind") == "confirmation"
    )
    executions = [event for event in deps.audit.events() if event.kind == KIND_EXECUTION]
    assert len(executions) == 2
    for event in executions:
        assert event.payload["user_id"] == deps.user_id
        assert event.payload["session_id"] == "query-audit-session"
        assert event.payload["execution_id"] == confirmation.payload["execution_id"]


@pytest.mark.parametrize(
    "failure", ["invalid_code", "binding_mismatch", "inactive", "expired"]
)
def test_challenge_authentication_anomalies_trip_global_breaker(
    deps: AppDeps, failure: str
) -> None:
    breaker = CircuitBreaker(threshold=1)
    configured = replace(
        deps,
        breaker=breaker,
        verification_limiter=VerificationAttemptLimiter(
            max_failures=10, lockout_seconds=60, backoff_seconds=(0,)
        ),
    )
    session, card = _red_card(configured)
    base = datetime(2026, 10, 1, 9, 0, 0)
    prompt = session.request_verification(now=base)
    code = prompt.code
    now = base
    if failure == "invalid_code":
        code = "000000" if code != "000000" else "111111"
    elif failure == "binding_mismatch":
        challenge = session._challenge
        assert challenge is not None
        session._challenge = replace(
            challenge,
            binding=replace(challenge.binding, user_id="other-user"),
        )
    elif failure == "inactive":
        challenge = session._challenge
        assert challenge is not None
        session._challenge = replace(challenge, status=ChallengeStatus.CONSUMED)
    else:
        now = base + timedelta(seconds=VERIFICATION_TTL_S + 1)

    with pytest.raises(StrongVerificationRequiredError):
        session.confirm(
            card.card_id, frozen_hash=card.frozen_hash, verification_code=code, now=now
        )

    assert breaker.is_open() is True
    assert BreakerTrigger.CONSECUTIVE_AUTH_FAILURES in breaker.triggers()
    breaker_event = next(
        event for event in deps.audit.events()
        if event.kind == KIND_BREAKER
        and event.payload.get("trigger") == BreakerTrigger.CONSECUTIVE_AUTH_FAILURES.value
    )
    assert breaker_event.payload["scope"] == "global"
    verification_event = next(
        event for event in deps.audit.events()
        if event.payload.get("kind") == "verification"
        and event.payload.get("status") in {"failed", "expired"}
    )
    assert verification_event.payload["reason"] == failure


def test_challenge_failure_retrip_after_unlock_is_audited(deps: AppDeps) -> None:
    configured = replace(
        _limited_deps(deps, max_failures=10),
        breaker=CircuitBreaker(threshold=1),
    )

    for cycle in range(2):
        session, card = _red_card(configured)
        prompt = session.request_verification()
        with pytest.raises(StrongVerificationRequiredError, match="不正确"):
            session.confirm(
                card.card_id,
                frozen_hash=card.frozen_hash,
                verification_code=_wrong_code(prompt.code),
            )

        events = [
            event for event in configured.audit.events()
            if event.kind == KIND_BREAKER
            and event.payload.get("trigger") == BreakerTrigger.CONSECUTIVE_AUTH_FAILURES.value
        ]
        assert len(events) == cycle + 1
        assert events[-1].payload["scope"] == "global"
        assert events[-1].payload["state"] == "open"
        assert events[-1].payload["user_id"] == configured.user_id
        assert events[-1].payload["session_id"] == session._session_id

        if cycle == 0:
            configured.breaker.unlock()


def test_strong_verification_rejects_a_wrong_code_then_accepts_the_right_one(
    deps: AppDeps,
) -> None:
    """错码被拒且不消耗挑战；正确码仍可执行（挑战由服务端下发，客户端无法自造）。"""
    session, card = _red_card(deps)
    prompt = session.request_verification()
    wrong = "000000" if prompt.code != "000000" else "111111"
    with pytest.raises(StrongVerificationRequiredError, match="不正确"):
        session.confirm(card.card_id, frozen_hash=card.frozen_hash, verification_code=wrong)
    done = session.confirm(
        card.card_id, frozen_hash=card.frozen_hash, verification_code=prompt.code
    )
    assert done.state is SessionState.COMPLETED


def test_strong_verification_requires_a_server_issued_challenge(deps: AppDeps) -> None:
    """未申请即提交任意 6 位数字：闸门拒绝（不接受任何客户端自造值）。"""
    session, card = _red_card(deps)
    with pytest.raises(StrongVerificationRequiredError, match="请先申请"):
        session.confirm(card.card_id, frozen_hash=card.frozen_hash, verification_code="123456")


def test_strong_verification_challenge_expires(deps: AppDeps) -> None:
    session, card = _red_card(deps)
    base = datetime(2026, 10, 1, 9, 0, 0)
    prompt = session.request_verification(now=base)
    with pytest.raises(StrongVerificationRequiredError, match="过期"):
        session.confirm(
            card.card_id,
            frozen_hash=card.frozen_hash,
            verification_code=prompt.code,
            now=base + timedelta(seconds=VERIFICATION_TTL_S + 1),
        )


def test_strong_verification_challenge_rotates_on_each_request(deps: AppDeps) -> None:
    """重复申请轮换验证码：旧码立即失效，新码可用。"""
    session, card = _red_card(deps)
    first = session.request_verification()
    second = session.request_verification()
    while second.code == first.code:  # 极低概率撞码：再申请一次
        second = session.request_verification()
    with pytest.raises(StrongVerificationRequiredError, match="不正确"):
        session.confirm(
            card.card_id, frozen_hash=card.frozen_hash, verification_code=first.code
        )
    done = session.confirm(
        card.card_id, frozen_hash=card.frozen_hash, verification_code=second.code
    )
    assert done.state is SessionState.COMPLETED


def test_strong_verification_is_rejected_for_non_red_cards(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("转给李娜 500 元")
    assert view.card is not None
    assert view.card.level is Level.YELLOW
    with pytest.raises(StrongVerificationRequiredError, match="无需强验证"):
        session.request_verification()


def test_open_breaker_routes_a_write_to_takeover(deps: AppDeps) -> None:
    session, card = _awaiting(deps)
    deps.breaker.lock()
    view = session.confirm(card.card_id, frozen_hash=card.frozen_hash)
    assert view.state is SessionState.TAKEN_OVER
    assert view.exec_results == ()


def test_open_breaker_does_not_block_a_pure_query(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("查看账单，再查看分类趋势")
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


def test_expired_plan_cannot_be_confirmed(deps: AppDeps, rw_store: Store) -> None:
    """G-02：服务端 `expires_at` 到期 ⇒ 确认失败、计划作废，且不产生任何写入。"""
    session = Session(deps)
    base = datetime(2026, 10, 1, 9, 0, 0)
    view = session.submit("转给李娜 500 元", now=base)
    card = view.card
    assert card is not None
    assert card.expires_at == base + timedelta(seconds=PLAN_TTL_S)
    with pytest.raises(SessionStateError, match="计划已过期"):
        session.confirm(
            card.card_id,
            frozen_hash=card.frozen_hash,
            now=base + timedelta(seconds=PLAN_TTL_S + 1),
        )
    assert session.state() is SessionState.BLOCKED
    # 终态由服务端控制：过期后无法用同一张卡「复活」
    with pytest.raises(SessionStateError):
        session.confirm(card.card_id, frozen_hash=card.frozen_hash)
    rows = rw_store.query(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = '转账' AND channel = 'agent'"
    )
    assert rows[0]["n"] == 0


def test_interrupted_plan_cannot_be_confirmed(deps: AppDeps) -> None:
    """G-02：`INTERRUPTED` 不是待确认状态，必须走恢复而不能直接确认。"""
    session, card = _awaiting(deps)
    session.interrupt()
    with pytest.raises(SessionStateError, match="当前状态不允许确认"):
        session.confirm(card.card_id, frozen_hash=card.frozen_hash)


def test_rejected_plan_cannot_be_confirmed(deps: AppDeps) -> None:
    """G-02：拒绝即作废，原计划与原卡片不得再执行。"""
    session, card = _awaiting(deps)
    rejected = session.reject()
    assert rejected.state is SessionState.IDLE
    with pytest.raises(SessionStateError, match="尚无已裁决计划"):
        session.confirm(card.card_id, frozen_hash=card.frozen_hash)


def test_changing_the_transaction_parameters_invalidates_the_previous_confirmation(
    deps: AppDeps, rw_store: Store
) -> None:
    """G-02：改参后重新裁决，旧卡（旧执行身份）不得再被确认。"""
    session = Session(deps)
    first = session.submit("转给李娜 500 元")
    assert first.card is not None
    second = session.submit("转给李娜 800 元")
    assert second.card is not None
    with pytest.raises(ConfirmationMismatchError, match="计划卡不匹配"):
        session.confirm(first.card.card_id, frozen_hash=first.card.frozen_hash)
    done = session.confirm(second.card.card_id, frozen_hash=second.card.frozen_hash)
    assert done.state is SessionState.COMPLETED
    rows = rw_store.query("SELECT balance_cents FROM accounts WHERE account_id = 'acc_savings'")
    assert rows[0]["balance_cents"] == 12843000 - 80000


def _limited_deps(
    deps: AppDeps, *, max_failures: int, backoff: tuple[int, ...] = (0,)
) -> AppDeps:
    """换上可预测阈值的限流器（状态仍在服务端 `AppDeps` 上，按用户共享）。"""
    return replace(
        deps,
        verification_limiter=VerificationAttemptLimiter(
            max_failures=max_failures, lockout_seconds=120, backoff_seconds=backoff
        ),
    )


def _wrong_code(code: str) -> str:
    return "000000" if code != "000000" else "111111"


def test_failed_codes_accumulate_across_challenge_rotation(deps: AppDeps) -> None:
    """重新申请（轮换挑战）不得重置失败次数——否则可无限重试。"""
    deps = _limited_deps(deps, max_failures=3)
    session, card = _red_card(deps)
    first = session.request_verification()
    with pytest.raises(StrongVerificationRequiredError, match="不正确"):
        session.confirm(
            card.card_id, frozen_hash=card.frozen_hash, verification_code=_wrong_code(first.code)
        )
    assert deps.verification_limiter.failures(deps.user_id) == 1

    second = session.request_verification()
    while second.code == first.code:  # 极低概率撞码：再申请一次
        second = session.request_verification()
    with pytest.raises(StrongVerificationRequiredError, match="不正确"):
        session.confirm(
            card.card_id, frozen_hash=card.frozen_hash, verification_code=_wrong_code(second.code)
        )
    assert deps.verification_limiter.failures(deps.user_id) == 2
    # 用当前挑战的正确码仍可完成（未达上限，合法用户不被卡死）。
    done = session.confirm(
        card.card_id, frozen_hash=card.frozen_hash, verification_code=second.code
    )
    assert done.state is SessionState.COMPLETED
    assert deps.verification_limiter.failures(deps.user_id) == 0


def test_locked_out_user_is_rejected_even_with_the_correct_code(
    deps: AppDeps, rw_store: Store
) -> None:
    """达到失败上限后进入锁定期：正确码也不再被比较，且不产生任何写入。"""
    deps = _limited_deps(deps, max_failures=2)
    session, card = _red_card(deps)
    prompt = session.request_verification()
    wrong = _wrong_code(prompt.code)
    for _ in range(2):
        with pytest.raises(StrongVerificationRequiredError, match="不正确"):
            session.confirm(card.card_id, frozen_hash=card.frozen_hash, verification_code=wrong)
    assert deps.verification_limiter.failures(deps.user_id) == 2

    with pytest.raises(StrongVerificationRequiredError, match="次数过多"):
        session.confirm(
            card.card_id, frozen_hash=card.frozen_hash, verification_code=prompt.code
        )
    # 锁定期内重新申请验证码同样被拒（换挑战不解除锁定）。
    with pytest.raises(StrongVerificationRequiredError, match="次数过多"):
        session.request_verification()
    assert session.state() is SessionState.AWAITING_CONFIRM
    rows = rw_store.query(
        "SELECT COUNT(*) AS n FROM transactions WHERE category = '转账' AND channel = 'agent'"
    )
    assert rows[0]["n"] == 0


def test_failed_codes_are_not_reset_by_switching_session(deps: AppDeps) -> None:
    """`session_id` 由客户端提供：换会话不得把同一用户的失败次数清零。"""
    deps = _limited_deps(deps, max_failures=2)
    first_session, card = _red_card(deps)
    prompt = first_session.request_verification()
    wrong = _wrong_code(prompt.code)
    for _ in range(2):
        with pytest.raises(StrongVerificationRequiredError, match="不正确"):
            first_session.confirm(
                card.card_id, frozen_hash=card.frozen_hash, verification_code=wrong
            )

    other_session = Session(deps, session_id="session-b")
    view = other_session.submit("转给李娜 1500 元")
    assert view.card is not None and view.card.level is Level.RED
    assert other_session._session_id != first_session._session_id
    # 换会话后申请验证码同样被拒：失败计数按用户、不按会话。
    with pytest.raises(StrongVerificationRequiredError, match="次数过多"):
        other_session.request_verification()
    assert deps.verification_limiter.failures(deps.user_id) == 2


@pytest.mark.parametrize("bad", [0, -1, MAX_CONFIRM_STEPS + 1, 1.5, True, "2"])
def test_confirm_rejects_out_of_range_steps(deps: AppDeps, bad: object) -> None:
    """`steps` 边界：拒绝 0 / 负数 / 超大值 / 浮点 / 布尔 / 数字字符串。"""
    session, card = _awaiting(deps)
    with pytest.raises(SessionStateError, match="steps 必须是"):
        session.confirm(
            card.card_id, frozen_hash=card.frozen_hash, steps=bad  # type: ignore[arg-type]
        )
    # 校验先于 `_require_pending`，不消耗任何状态：随后正常确认仍可执行。
    done = session.confirm(card.card_id, frozen_hash=card.frozen_hash)
    assert done.state is SessionState.COMPLETED


def test_confirm_accepts_the_upper_bound_for_steps(deps: AppDeps) -> None:
    """合法上界不改变业务行为：执行全部待执行节点。"""
    session = Session(deps)
    view = session.submit("查看账单，再查看分类趋势")
    assert view.card is not None
    done = session.confirm(
        view.card.card_id, frozen_hash=view.card.frozen_hash, steps=MAX_CONFIRM_STEPS
    )
    assert done.state is SessionState.COMPLETED
    assert len(done.exec_results) == 2


def test_changing_the_payer_account_currency_invalidates_the_previous_confirmation(
    deps: AppDeps, rw_store: Store
) -> None:
    """任务 6：付款账户币种变化 ⇒ 冻结参数变化 ⇒ 旧卡（旧 frozen_hash）不得再被确认。"""
    session = Session(deps)
    first = session.submit("转给李娜 500 元")
    assert first.card is not None
    with rw_store.transaction() as conn:
        conn.execute("UPDATE accounts SET currency = 'USD' WHERE account_id = 'acc_savings'")
    second = session.submit("转给李娜 500 元")
    assert second.card is not None
    assert second.card.frozen_hash != first.card.frozen_hash
    assert "（USD）" in second.card.lines[0]
    with pytest.raises(ConfirmationMismatchError, match="计划卡不匹配"):
        session.confirm(first.card.card_id, frozen_hash=first.card.frozen_hash)
    done = session.confirm(second.card.card_id, frozen_hash=second.card.frozen_hash)
    assert done.state is SessionState.COMPLETED
