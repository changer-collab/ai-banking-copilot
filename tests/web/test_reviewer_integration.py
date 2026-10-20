from dataclasses import replace
from datetime import date

import httpx
import pytest

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.contracts.plan import AnchorSource
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.merchant_store import init_merchant_schema, seed_user_profiles
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.transfer_store import init_transfer_schema
from ai_banking_copilot.planning.reviewer import (
    ReviewReport,
    ReviewRequest,
    ReviewStatus,
    StrictPlanReviewer,
)
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.web.session import Session, SessionState


class _CapturingReviewer:
    def __init__(self) -> None:
        self.requests: list[ReviewRequest] = []

    def review(self, request: ReviewRequest) -> ReviewReport:
        self.requests.append(request)
        return ReviewReport(
            execution_id=request.execution_id,
            cfhash=request.cfhash,
            prompt_version=request.prompt_version,
            status=ReviewStatus.ALLOW,
        )


class _RejectingReviewer:
    def review(self, request: ReviewRequest) -> ReviewReport:
        return ReviewReport(
            execution_id=request.execution_id,
            cfhash=request.cfhash,
            prompt_version=request.prompt_version,
            status=ReviewStatus.REJECT,
            findings=("测试审查拒绝",),
        )


def test_required_reviewer_takes_over_write_plan(deps: object) -> None:
    configured = replace(deps, reviewer=_RejectingReviewer(), reviewer_required=True)
    view = Session(configured).submit("转给李娜 500 元", selected={"account": "acc_savings"})
    assert view.state is SessionState.TAKEN_OVER
    assert view.card is not None


def test_reviewer_read_timeout_keeps_safe_diagnostics_and_never_writes(
    deps: object, audit: object, rw_store: object,
) -> None:
    def transport(_request: str) -> str:
        raise httpx.ReadTimeout("do-not-log-provider-secret")

    reviewer = StrictPlanReviewer(transport=transport)
    before = tuple(tuple(row) for row in rw_store.query("SELECT * FROM accounts"))
    session = Session(replace(deps, reviewer=reviewer, reviewer_required=True))
    view = session.submit("转给李娜 500 元", selected={"account": "acc_savings"})
    assert view.state is SessionState.TAKEN_OVER
    assert tuple(tuple(row) for row in rw_store.query("SELECT * FROM accounts")) == before
    assert not rw_store.query("SELECT * FROM transactions WHERE channel='agent'")
    review_event = next(event for event in audit.events() if "review_status" in event.payload)
    assert review_event.payload["review_failure"] == "timeout"
    assert review_event.payload["review_error_type"] == "ReadTimeout"
    assert review_event.payload["review_http_status"] is None
    assert review_event.payload["review_timeout_s"] == 180.0
    assert "do-not-log-provider-secret" not in audit.path.read_text(encoding="utf-8")
    assert audit.verify_chain()


def test_required_reviewer_does_not_change_pure_query_semantics(deps: object) -> None:
    configured = replace(deps, reviewer=_RejectingReviewer(), reviewer_required=True)
    view = Session(configured).submit("查余额", selected={"account": "acc_savings"})
    assert view.state is SessionState.AWAITING_CONFIRM
    assert view.card is not None
    assert view.card.level is Level.GREEN


def test_reviewer_sees_accepted_slots_sources_and_related_gateway_facts(deps: object) -> None:
    reviewer = _CapturingReviewer()
    session = Session(replace(deps, reviewer=reviewer))
    view = session.submit("转给李娜 500 元", selected={"account": "acc_savings"})
    assert view.state is SessionState.AWAITING_CONFIRM
    context = reviewer.requests[-1].slot_sources
    assert context["request"]["authorization"] == "pending_confirmation"
    intent = context["intents"][0]
    assert intent["slots"]["account"] == "acc_savings"
    assert intent["slots"]["payee"] == "p_lina"
    assert intent["slots"]["amount"] == 50000
    assert intent["selected_sources"]["account"] == "user_selection"
    assert "resolved payee:p_lina" in intent["trace"]
    assert "resolved amount:50000" in intent["trace"]
    facts = {fact["record_id"]: fact for fact in context["facts"]}
    assert facts["accounts:acc_savings"]["user_id"] == deps.user_id
    assert facts["payees:p_lina"]["full_name"] == "李娜"
    assert facts["payees:p_lina"]["user_id"] == deps.user_id
    assert facts["payees:p_lina"]["identity_confirmed"] is True


def test_reviewer_distinguishes_clarification_and_drops_stale_selection(deps: object) -> None:
    reviewer = _CapturingReviewer()
    session = Session(replace(deps, reviewer=reviewer))
    missing = session.submit("取消订阅")
    assert missing.state is SessionState.NEEDS_CLARIFICATION
    clarified = session.clarify({"subscription": "sub_video"})
    assert clarified.state is SessionState.AWAITING_CONFIRM
    intent = reviewer.requests[-1].slot_sources["intents"][0]
    assert intent["slots"]["subscription"] == "sub_video"
    assert intent["selected_sources"]["subscription"] == "clarification_answer"

    session.submit("转给李娜 500 元")
    fresh = reviewer.requests[-1].slot_sources
    assert fresh["intents"][0]["slots"].get("subscription") is None
    assert fresh["intents"][0]["selected_sources"].get("subscription") is None


def test_two_clarifications_keep_prior_answer_source_and_server_identity(deps: object) -> None:
    reviewer = _CapturingReviewer()
    session = Session(replace(deps, reviewer=reviewer))
    first = session.submit("转给李娜")
    assert first.state is SessionState.NEEDS_CLARIFICATION
    second = session.clarify({"account": "acc_savings", "user_id": "u_other"})
    assert second.state is SessionState.NEEDS_CLARIFICATION
    third = session.clarify({"amount": 50000})
    assert third.state is SessionState.AWAITING_CONFIRM
    intent = reviewer.requests[-1].slot_sources["intents"][0]
    assert intent["selected_sources"]["account"] == "clarification_answer"
    assert intent["selected_sources"]["amount"] == "clarification_answer"
    assert intent["selected_sources"]["user_id"] == "server_identity"
    assert intent["slots"]["user_id"] == deps.user_id
    session.clarify({})
    repeated = reviewer.requests[-1].slot_sources["intents"][0]
    assert repeated["selected_sources"]["account"] == "clarification_answer"
    assert repeated["selected_sources"]["amount"] == "clarification_answer"


def test_unverified_selection_is_not_presented_as_accepted_control_evidence(deps: object) -> None:
    reviewer = _CapturingReviewer()
    session = Session(replace(deps, reviewer=reviewer))
    view = session.submit(
        "转给李娜 500 元",
        selected={"account": Value("acc_savings", Trust.UNVERIFIED)},
    )
    assert view.state is SessionState.NEEDS_CLARIFICATION
    assert reviewer.requests == []


def test_due_review_uses_current_records_and_stays_unapproved(
    deps: object, core_bank: object, rw_store: object
) -> None:
    init_merchant_schema(rw_store)
    seed_user_profiles(rw_store)
    reviewer = _CapturingReviewer()
    core_bank.book_transfer(
        user_id=deps.user_id,
        account_id="acc_savings",
        payee_id="p_lina",
        amount=Money(10000),
        booked_on=date(2026, 10, 1),
        remark="",
        idempotency_key="review-due",
    )
    session = Session(replace(deps, reviewer=reviewer))
    session.submit("取消订阅", selected={"subscription": "sub_video"})
    due = session.check_due()
    assert due.state is SessionState.AWAITING_CONFIRM
    assert due.plan is not None
    assert all(node.intent.source is AnchorSource.EXPLICIT for node in due.plan.nodes)
    request = reviewer.requests[-1]
    assert request.slot_sources["request"]["kind"] == "due_proposal"
    assert request.slot_sources["request"]["authorization"] == "pending_confirmation"
    assert request.slot_sources["intents"] == []
    assert "sub_video" not in str(request.slot_sources)
    assert "确认" in request.plan_summary
    facts = {
        fact["record_id"]: fact for fact in request.slot_sources["facts"]
        if "record_id" in fact
    }
    assert any(key.startswith("scheduled_transfers:") for key in facts)
    assert all(fact["content_hash"] for fact in facts.values() if "record_id" in fact)
    schedule = next(fact for key, fact in facts.items() if key.startswith("scheduled_transfers:"))
    assert schedule["amount_cents"] == 10000
    assert schedule["booked_on"] == "2026-10-01"
    assert deps.gateway.scheduled_transfer(schedule["schedule_id"]).payload.status == "scheduled"
    assert facts["user_profiles:u_demo"]["birthday"] == "10-03"
    assert any(fact.get("source") == "demo_configuration" for fact in request.slot_sources["facts"])


def test_due_confirmation_requires_frozen_hash_and_executes_once(
    deps: object, core_bank: object, rw_store: object
) -> None:
    init_merchant_schema(rw_store)
    core_bank.book_transfer(
        user_id=deps.user_id,
        account_id="acc_savings",
        payee_id="p_lina",
        amount=Money(10000),
        booked_on=date(2026, 10, 1),
        remark="",
        idempotency_key="review-due-once",
    )
    session = Session(deps)
    due = session.check_due()
    assert due.card is not None
    assert due.plan is not None
    schedule_id = next(
        str(node.params["schedule_id"])
        for node in due.plan.nodes if "schedule_id" in node.params
    )
    assert deps.gateway.scheduled_transfer(schedule_id).payload.status == "scheduled"
    with pytest.raises(StalePlanError, match="哈希不匹配"):
        session.confirm(due.card.card_id, frozen_hash="wrong")
    assert deps.gateway.scheduled_transfer(schedule_id).payload.status == "scheduled"
    done = session.confirm(due.card.card_id, frozen_hash=due.card.frozen_hash)
    assert done.state is SessionState.COMPLETED
    assert deps.gateway.scheduled_transfer(schedule_id).payload.status == "completed"
    assert session.check_due().plan is None


def test_due_reviewer_question_requests_recheck_without_reusing_previous_command(
    deps: object, rw_store: object
) -> None:
    class ClarifyingReviewer(_CapturingReviewer):
        def review(self, request: ReviewRequest) -> ReviewReport:
            return replace(
                super().review(request), status=ReviewStatus.CLARIFY,
                clarifications=("请核对到期登记",),
            )

    init_transfer_schema(rw_store)
    init_merchant_schema(rw_store)
    seed_user_profiles(rw_store)
    session = Session(replace(deps, reviewer=ClarifyingReviewer(), reviewer_required=True))
    session.submit("取消订阅", selected={"subscription": "sub_video"})
    due = session.check_due()
    assert due.state is SessionState.TAKEN_OVER
    assert "重新检查" in due.message
    assert deps.gateway.subscription("sub_video").payload.status == "active"
