"""身份边界流程回归；严格 Reviewer 注入固定响应，不验证真实模型判断能力。"""

import json
from dataclasses import replace

import pytest

from ai_banking_copilot.contracts.authorization import Level, ReasonCode
from ai_banking_copilot.planning.reviewer import StrictPlanReviewer
from ai_banking_copilot.web.plan_card import PROMPT_USER_CONFIRM
from ai_banking_copilot.web.session import Session, SessionState, SessionStateError


def _strict_response(
    status: str, captured: list[dict[str, object]],
) -> StrictPlanReviewer:
    def transport(raw: str) -> dict[str, object]:
        request = json.loads(raw)
        captured.append(request)
        return {
            **{key: request[key] for key in ("execution_id", "cfhash", "prompt_version")},
            "status": status,
            "findings": [],
            "clarifications": [],
        }

    return StrictPlanReviewer(transport=transport)


def _business_rows(store: object) -> dict[str, tuple[tuple[object, ...], ...]]:
    return {
        table: tuple(tuple(row) for row in store.query(f"SELECT * FROM {table}"))
        for table in ("accounts", "payees", "transactions")
    }


def test_allow_for_unique_unverified_payee_keeps_yellow_confirmation_and_identity(
    deps: object, rw_store: object,
) -> None:
    captured: list[dict[str, object]] = []
    reviewer = _strict_response("allow", captured)
    session = Session(replace(deps, reviewer=reviewer, reviewer_required=True))
    before = _business_rows(rw_store)
    view = session.submit("转给张强 100 元", selected={"account": "acc_savings"})

    assert view.state is SessionState.AWAITING_CONFIRM
    assert view.decision.level is Level.YELLOW
    assert ReasonCode.MANUAL_REVIEW in view.decision.reasons
    assert view.card.requires == (PROMPT_USER_CONFIRM,)
    assert any("张强" in line for line in view.card.lines)
    assert view.plan.nodes[0].params["payee_id"] == "p_zhangqiang"
    assert view.plan.nodes[0].params["amount_cents"] == 10000
    assert len(captured) == 1
    facts = {fact["record_id"]: fact for fact in captured[0]["slot_sources"]["facts"]}
    assert facts["payees:p_zhangqiang"]["identity_confirmed"] is False
    assert _business_rows(rw_store) == before

    result = session.confirm(view.card.card_id, frozen_hash=view.card.frozen_hash)
    assert result.state is SessionState.COMPLETED
    assert len(rw_store.query("SELECT * FROM transactions WHERE channel = 'agent'")) == 1
    assert tuple(tuple(row) for row in rw_store.query("SELECT * FROM payees")) == before["payees"]


@pytest.mark.parametrize("utterance", ["转给王伟 100 元", "转给孙七 100 元"])
def test_ambiguous_or_unknown_payee_never_reaches_an_allowing_reviewer(
    deps: object, rw_store: object, utterance: str,
) -> None:
    captured: list[dict[str, object]] = []
    session = Session(replace(
        deps, reviewer=_strict_response("allow", captured), reviewer_required=True,
    ))
    before = _business_rows(rw_store)
    view = session.submit(utterance, selected={"account": "acc_savings"})

    assert view.state is SessionState.NEEDS_CLARIFICATION
    assert view.plan is None and view.card is None
    assert any(ask.slot == "payee" for ask in view.clarifications)
    assert captured == []
    assert _business_rows(rw_store) == before


def test_reviewer_allow_cannot_override_blocked_payee_denial(
    deps: object, rw_store: object,
) -> None:
    captured: list[dict[str, object]] = []
    session = Session(replace(
        deps, reviewer=_strict_response("allow", captured), reviewer_required=True,
    ))
    before = _business_rows(rw_store)
    view = session.submit("转给赵六 100 元", selected={"account": "acc_savings"})

    assert len(captured) == 1
    assert session.review_report().status.value == "allow"
    assert view.state is SessionState.BLOCKED
    assert view.decision.level is Level.DENY
    assert ReasonCode.OBJECT_BLOCKED in view.decision.reasons
    with pytest.raises(SessionStateError):
        session.confirm(view.card.card_id, frozen_hash=view.card.frozen_hash)
    assert _business_rows(rw_store) == before


@pytest.mark.parametrize(
    "utterance, selected, status, expected_state, expected_payee",
    [
        (
            "只转给身份已核实的张强 100 元", {"account": "acc_savings"},
            "clarify", SessionState.NEEDS_CLARIFICATION, "p_zhangqiang",
        ),
        (
            "转给张强 100 元", {"account": "acc_savings", "payee": "p_lina"},
            "reject", SessionState.TAKEN_OVER, "p_lina",
        ),
    ],
)
def test_session_preserves_strict_clarify_or_reject_for_identity_or_object_conflict(
    deps: object, rw_store: object, utterance: str, selected: dict[str, str],
    status: str, expected_state: SessionState, expected_payee: str,
) -> None:
    captured: list[dict[str, object]] = []
    session = Session(replace(
        deps, reviewer=_strict_response(status, captured), reviewer_required=True,
    ))
    before = _business_rows(rw_store)
    view = session.submit(utterance, selected=selected)

    assert len(captured) == 1
    assert captured[0]["raw_text"] == utterance
    assert captured[0]["plan"]["nodes"][0]["params"]["payee_id"] == expected_payee
    assert session.review_report().status.value == status
    assert view.state is expected_state
    assert _business_rows(rw_store) == before
