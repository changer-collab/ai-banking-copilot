from dataclasses import replace

import pytest

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.planning.llm import Intent, IntentProposal
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session, SessionState


def _confirm(session: Session) -> None:
    card = session.card()
    assert card is not None
    code = session.request_verification().code if card.level is Level.RED else None
    done = session.confirm(card.card_id, frozen_hash=card.frozen_hash, verification_code=code)
    assert done.state is SessionState.COMPLETED, done.message


def test_multi_action_without_distinct_source_spans_requires_clarification(deps: AppDeps) -> None:
    class _Model:
        def propose(self, utterance: str, context: object) -> IntentProposal:
            return IntentProposal(
                intents=(
                    Intent(action="funds.freeze", confidence=0.9),
                    Intent(action="transfer.create", confidence=0.9),
                ),
                raw=utterance,
            )

    session = Session(replace(deps, intent_model=_Model()))
    view = session.submit("冻结 500 元，转给李娜 300 元", selected={"account": "acc_savings"})
    assert view.state is SessionState.NEEDS_CLARIFICATION
    assert view.plan is None
    assert len(view.clarifications) == 2
    assert "第 1 项" in view.clarifications[0].prompt
    assert "intent_" not in view.message
    initial = deps.gateway.account("acc_savings").payload.balance.cents
    resolved = session.clarify({
        "intent_0_span": "冻结 500 元", "intent_1_span": "转给李娜 300 元",
    })
    assert resolved.state is SessionState.AWAITING_CONFIRM, resolved.message
    _confirm(session)
    # 冻结金额也会从可用余额中扣除：50000（冻结）+30000（转账）。
    assert deps.gateway.account("acc_savings").payload.balance.cents == initial - 80000
    holds = deps.gateway.active_holds_of(deps.user_id)
    assert len(holds) == 1
    assert holds[0].payload.amount.cents == 50000


def test_booking_marker_cannot_be_planned_as_immediate_transfer(deps: AppDeps) -> None:
    class _Model:
        def propose(self, utterance: str, context: object) -> IntentProposal:
            return IntentProposal(
                intents=(
                    Intent(
                        action="transfer.schedule",
                        slots={"span": utterance},
                        confidence=0.9,
                    ),
                ),
                raw=utterance,
            )

    session = Session(replace(deps, intent_model=_Model()))
    initial = deps.gateway.account("acc_savings").payload.balance.cents
    view = session.submit(
        "登记预约转账 给李娜 500 元 2026-10-01",
        selected={"account": "acc_savings"},
    )
    assert view.state is SessionState.AWAITING_CONFIRM
    assert view.plan is not None
    assert view.plan.nodes[0].action == "transfer.book"
    _confirm(session)
    assert deps.gateway.account("acc_savings").payload.balance.cents == initial
    booked = deps.gateway.scheduled_transfers_of(deps.user_id, status="scheduled")
    assert len(booked) == 1
    assert booked[0].payload.amount.cents == 50000


@pytest.mark.parametrize("answer,delta", [("登记", 0), ("立即转账", -50000)])
def test_booking_conflict_can_be_resolved_with_chinese_choice(
    deps: AppDeps, answer: str, delta: int,
) -> None:
    class _Model:
        def propose(self, utterance: str, context: object) -> IntentProposal:
            return IntentProposal(
                intents=(
                    Intent(
                        action="transfer.schedule",
                        slots={"span": utterance},
                        confidence=0.9,
                    ),
                ),
                raw=utterance,
            )

    session = Session(replace(deps, intent_model=_Model()))
    initial = deps.gateway.account("acc_savings").payload.balance.cents
    first = session.submit(
        "登记预约转账，现在立即转账给李娜 500 元 2026-10-01",
        selected={"account": "acc_savings"},
    )
    assert first.state is SessionState.NEEDS_CLARIFICATION
    assert first.plan is None
    resolved = session.clarify({"action": answer})
    assert resolved.state is SessionState.AWAITING_CONFIRM, resolved.message
    _confirm(session)
    assert deps.gateway.account("acc_savings").payload.balance.cents == initial + delta


def test_only_booking_transfer_is_corrected_with_another_immediate_transfer(deps: AppDeps) -> None:
    class _Model:
        def propose(self, utterance: str, context: object) -> IntentProposal:
            return IntentProposal(
                intents=(
                    Intent(
                        action="transfer.schedule",
                        slots={"span": "登记预约转账给李娜 500 元 2026-10-01"},
                        confidence=0.9,
                    ),
                    Intent(
                        action="transfer.create",
                        slots={"span": "立即转账给李娜 300 元"},
                        confidence=0.9,
                    ),
                ),
                raw=utterance,
            )

    session = Session(replace(deps, intent_model=_Model()))
    initial = deps.gateway.account("acc_savings").payload.balance.cents
    view = session.submit(
        "登记预约转账给李娜 500 元 2026-10-01，然后立即转账给李娜 300 元",
        selected={"account": "acc_savings"},
    )
    assert view.state is SessionState.AWAITING_CONFIRM, view.message
    assert view.plan is not None
    assert [node.action for node in view.plan.nodes] == ["transfer.book", "transfer.create"]
    _confirm(session)
    assert deps.gateway.account("acc_savings").payload.balance.cents == initial - 30000
    booked = deps.gateway.scheduled_transfers_of(deps.user_id, status="scheduled")
    assert len(booked) == 1
    assert booked[0].payload.amount.cents == 50000


@pytest.mark.parametrize("multiple", [False, True])
def test_shortened_model_span_cannot_remove_booking_semantics(
    deps: AppDeps, multiple: bool,
) -> None:
    class _Model:
        def propose(self, utterance: str, context: object) -> IntentProposal:
            intents = (
                Intent(
                    action="transfer.schedule",
                    slots={"span": "转账 给李娜 500 元 2026-10-01"},
                    confidence=0.9,
                ),
            )
            if multiple:
                intents += (Intent(
                    action="transfer.create",
                    slots={"span": "立即转账给李娜 300 元"},
                    confidence=0.9,
                ),)
            return IntentProposal(intents=intents, raw=utterance)

    session = Session(replace(deps, intent_model=_Model()))
    initial = deps.gateway.account("acc_savings").payload.balance.cents
    utterance = "登记预约转账 给李娜 500 元 2026-10-01"
    if multiple:
        utterance += "，立即转账给李娜 300 元"
    view = session.submit(utterance, selected={"account": "acc_savings"})
    assert view.state is SessionState.AWAITING_CONFIRM, view.message
    assert view.plan is not None
    assert view.plan.nodes[0].action == "transfer.book"
    _confirm(session)
    delta = 30000 if multiple else 0
    assert deps.gateway.account("acc_savings").payload.balance.cents == initial - delta
    booked = deps.gateway.scheduled_transfers_of(deps.user_id, status="scheduled")
    assert len(booked) == 1
    assert booked[0].payload.amount.cents == 50000


def test_multiple_missing_amounts_require_separate_answers(deps: AppDeps) -> None:
    class _Model:
        def propose(self, utterance: str, context: object) -> IntentProposal:
            return IntentProposal(
                intents=(
                    Intent(action="funds.freeze", slots={"span": "冻结一笔钱"}, confidence=0.9),
                    Intent(
                        action="transfer.create", slots={"span": "转给李娜一笔钱"}, confidence=0.9,
                    ),
                ),
                raw=utterance,
            )

    session = Session(replace(deps, intent_model=_Model()))
    first = session.submit("冻结一笔钱，然后转给李娜一笔钱", selected={"account": "acc_savings"})
    assert first.state is SessionState.NEEDS_CLARIFICATION
    assert [question.slot for question in first.clarifications] == [
        "intent_0_amount", "intent_1_amount",
    ]
    shared = session.clarify({"amount": "500元"})
    assert shared.state is SessionState.NEEDS_CLARIFICATION
    initial = deps.gateway.account("acc_savings").payload.balance.cents
    resolved = session.clarify({"intent_0_amount": "500元", "intent_1_amount": "300元"})
    assert resolved.state is SessionState.AWAITING_CONFIRM, resolved.message
    _confirm(session)
    assert deps.gateway.account("acc_savings").payload.balance.cents == initial - 80000
    assert deps.gateway.active_holds_of(deps.user_id)[0].payload.amount.cents == 50000


def test_shared_amount_does_not_override_amounts_in_separate_spans(deps: AppDeps) -> None:
    session = Session(deps)
    initial = deps.gateway.account("acc_savings").payload.balance.cents
    view = session.submit(
        "冻结500元，然后转给李娜300元",
        selected={"account": "acc_savings", "amount": "900元"},
    )
    assert view.state is SessionState.AWAITING_CONFIRM, view.message
    _confirm(session)
    assert deps.gateway.account("acc_savings").payload.balance.cents == initial - 80000
    assert deps.gateway.active_holds_of(deps.user_id)[0].payload.amount.cents == 50000


@pytest.mark.parametrize(
    "utterance",
    ["预约明天转给李娜500元", "只登记转账给李娜500元不要立即扣款"],
)
def test_common_booking_forms_do_not_debit_on_registration(
    deps: AppDeps, utterance: str,
) -> None:
    class _Model:
        def propose(self, utterance: str, context: object) -> IntentProposal:
            return IntentProposal(
                intents=(
                    Intent(action="transfer.create", slots={"span": utterance}, confidence=0.9),
                ),
                raw=utterance,
            )

    session = Session(replace(deps, intent_model=_Model()))
    initial = deps.gateway.account("acc_savings").payload.balance.cents
    view = session.submit(utterance, selected={"account": "acc_savings"})
    assert view.state is SessionState.AWAITING_CONFIRM, view.message
    _confirm(session)
    assert deps.gateway.account("acc_savings").payload.balance.cents == initial
    assert len(deps.gateway.scheduled_transfers_of(deps.user_id, status="scheduled")) == 1


def test_shortened_span_cannot_remove_negation(deps: AppDeps) -> None:
    class _Model:
        def propose(self, utterance: str, context: object) -> IntentProposal:
            return IntentProposal(
                intents=(
                    Intent(action="funds.freeze", slots={"span": "冻结500元"}, confidence=0.9),
                    Intent(
                        action="transfer.create", slots={"span": "转给李娜300元"}, confidence=0.9,
                    ),
                ),
                raw=utterance,
            )

    session = Session(replace(deps, intent_model=_Model()))
    initial = deps.gateway.account("acc_savings").payload.balance.cents
    view = session.submit("冻结500元，然后不要转给李娜300元", selected={"account": "acc_savings"})
    assert view.state is SessionState.AWAITING_CONFIRM, view.message
    assert view.plan is not None
    assert [node.action for node in view.plan.nodes] == ["funds.freeze"]
    _confirm(session)
    assert deps.gateway.account("acc_savings").payload.balance.cents == initial - 50000


@pytest.mark.parametrize("another_transfer", [False, True])
def test_unassigned_booking_prefix_requires_local_transfer_clarification(
    deps: AppDeps, another_transfer: bool,
) -> None:
    class _Model:
        def propose(self, utterance: str, context: object) -> IntentProposal:
            intents = (
                Intent(
                    action="transfer.schedule",
                    slots={"span": "明天给李娜转账500元"},
                    confidence=0.9,
                ),
                Intent(action="funds.freeze", slots={"span": "冻结300元"}, confidence=0.9),
            )
            if another_transfer:
                intents += (Intent(
                    action="transfer.create", slots={"span": "转给李娜100元"}, confidence=0.9,
                ),)
            return IntentProposal(intents=intents, raw=utterance)

    session = Session(replace(deps, intent_model=_Model()))
    initial = deps.gateway.account("acc_savings").payload.balance.cents
    utterance = "只登记，明天给李娜转账500元，再冻结300元"
    if another_transfer:
        utterance += "，然后转给李娜100元"
    first = session.submit(utterance, selected={"account": "acc_savings"})
    assert first.state is SessionState.NEEDS_CLARIFICATION
    assert first.plan is None
    assert [question.slot for question in first.clarifications] == ["intent_0_action"]
    assert deps.gateway.account("acc_savings").payload.balance.cents == initial
    shared_choice = session.clarify({"action": "立即转账"})
    assert shared_choice.state is SessionState.NEEDS_CLARIFICATION
    ready = session.clarify({"intent_0_action": "登记"})
    assert ready.state is SessionState.AWAITING_CONFIRM, ready.message
    assert ready.plan is not None
    expected = [("transfer.book", 50000), ("funds.freeze", 30000)]
    if another_transfer:
        # 跨场景装配先列出转账域节点，再列出冻结资金节点。
        expected.insert(1, ("transfer.create", 10000))
    assert [(node.action, node.params["amount_cents"]) for node in ready.plan.nodes] == expected
    _confirm(session)
    delta = 30000 + (10000 if another_transfer else 0)
    assert deps.gateway.account("acc_savings").payload.balance.cents == initial - delta
    booked = deps.gateway.scheduled_transfers_of(deps.user_id, status="scheduled")
    assert len(booked) == 1
    assert booked[0].payload.amount.cents == 50000


def test_single_transfer_keeps_full_booking_prefix_context(deps: AppDeps) -> None:
    class _Model:
        def propose(self, utterance: str, context: object) -> IntentProposal:
            return IntentProposal(
                intents=(Intent(
                    action="transfer.schedule", slots={"span": "明天给李娜转账500元"},
                    confidence=0.9,
                ),),
                raw=utterance,
            )

    session = Session(replace(deps, intent_model=_Model()))
    initial = deps.gateway.account("acc_savings").payload.balance.cents
    view = session.submit("只登记，明天给李娜转账500元", selected={"account": "acc_savings"})
    assert view.state is SessionState.AWAITING_CONFIRM, view.message
    _confirm(session)
    assert deps.gateway.account("acc_savings").payload.balance.cents == initial
    assert len(deps.gateway.scheduled_transfers_of(deps.user_id, status="scheduled")) == 1
