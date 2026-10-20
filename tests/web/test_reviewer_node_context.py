"""逐节点审查出处回归；只使用本地网关和捕获器，不调用模型。"""

from dataclasses import replace
from datetime import date

import pytest

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.merchant_store import init_merchant_schema, seed_user_profiles
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.transfer_store import init_transfer_schema
from ai_banking_copilot.planning.reviewer import (
    ReviewReport,
    ReviewRequest,
    ReviewStatus,
    build_reviewer_chat_payload,
)
from ai_banking_copilot.scenarios.bills import BillsHandler
from ai_banking_copilot.scenarios.pending import PendingHandler
from ai_banking_copilot.web.session import Session, SessionState


class CapturingReviewer:
    def __init__(self) -> None:
        self.requests: list[ReviewRequest] = []

    def review(self, request: ReviewRequest) -> ReviewReport:
        self.requests.append(request)
        return ReviewReport(
            execution_id=request.execution_id, cfhash=request.cfhash,
            prompt_version=request.prompt_version, status=ReviewStatus.ALLOW,
        )


def test_due_nodes_link_independent_facts_and_rule_without_authorizing(
    deps: object, core_bank: object, rw_store: object,
) -> None:
    init_merchant_schema(rw_store)
    seed_user_profiles(rw_store)
    for index, (account, payee, cents) in enumerate((
        ("acc_savings", "p_lina", 10000), ("acc_current", "p_wangwei_a", 20000),
    )):
        core_bank.book_transfer(
            user_id=deps.user_id, account_id=account, payee_id=payee,
            amount=Money(cents), booked_on=date(2026, 9, 30), remark="",
            idempotency_key=f"node-context-{index}",
        )
    before = {
        table: tuple(tuple(row) for row in rw_store.query(f"SELECT * FROM {table}"))
        for table in ("accounts", "transactions", "scheduled_transfers", "merchant_orders")
    }
    reviewer = CapturingReviewer()
    view = Session(replace(deps, reviewer=reviewer, reviewer_required=True)).check_due()
    assert view.state is SessionState.AWAITING_CONFIRM
    request = reviewer.requests[0]
    assert request.prompt_version == "reviewer-v3"
    facts = {fact["record_id"]: fact for fact in request.slot_sources["facts"]}
    assert "accounts:acc_savings" in facts
    assert "payees:p_lina" in facts
    assert "payees:p_zhaoliu" not in facts
    contexts = request.slot_sources["node_context"]
    assert set(contexts) == {node.node_id for node in request.plan.nodes}
    for node in request.plan.nodes:
        context = contexts[node.node_id]
        assert context["authorization"] == "pending_confirmation"
        assert context["intent_scope"] == "check_due_proposal"
        assert context["missing_sources"] == []
        basis = context["proposal_basis"]
        if node.action == "transfer.create":
            schedule_ref = f"scheduled_transfers:{node.params['schedule_id']}"
            assert set(context["fact_refs"]) == {
                f"accounts:{node.params['account_id']}",
                f"payees:{node.params['payee_id']}", schedule_ref,
            }
            assert facts[schedule_ref]["amount_cents"] == node.params["amount_cents"]
            assert facts[schedule_ref]["booked_on"] == "2026-09-30"
            assert basis["parameter_sources"]["executed_on"] == "request.today"
            assert "executed_on" not in facts[schedule_ref]
        else:
            assert node.action == "merchant.order"
            assert set(context["fact_refs"]) == {
                "user_profiles:u_demo", "demo_configuration:birthday_order",
            }
            assert basis["birthday_on"] == "2026-10-03"
            assert basis["order_on"] == "2026-10-01"
            assert basis["occasion_completed"] is False
            assert basis["source"] == "server_rule"
    assert len(facts) == len(request.slot_sources["facts"])
    for table, rows in before.items():
        assert tuple(tuple(row) for row in rw_store.query(f"SELECT * FROM {table}")) == rows
    prompt = build_reviewer_chat_payload(request, model="local")["messages"][0]["content"]
    assert "node_context" in prompt
    assert "PDP" in prompt and "PEP" in prompt
    assert "explicit" in prompt and "不得授予执行权限" in prompt


def test_due_evidence_is_reread_instead_of_copying_plan_parameters(
    deps: object, core_bank: object, rw_store: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_merchant_schema(rw_store)
    core_bank.book_transfer(
        user_id=deps.user_id, account_id="acc_savings", payee_id="p_lina",
        amount=Money(10000), booked_on=date(2026, 10, 1), remark="",
        idempotency_key="node-context-independent",
    )
    original = PendingHandler.build

    def changed_plan(self: PendingHandler, intents: object, gateway: FactGateway) -> object:
        plan = original(self, intents, gateway)
        node = plan.nodes[0]
        return replace(plan, nodes=(replace(node, params={**node.params, "amount_cents": 20000}),))

    monkeypatch.setattr(PendingHandler, "build", changed_plan)
    reviewer = CapturingReviewer()
    Session(replace(deps, reviewer=reviewer)).check_due()
    request = reviewer.requests[0]
    assert request.plan.nodes[0].params["amount_cents"] == 20000
    schedule = next(
        fact for fact in request.slot_sources["facts"] if "schedule_id" in fact
    )
    assert schedule["amount_cents"] == 10000
    assert schedule["record_id"] in request.slot_sources["node_context"]["due1"]["fact_refs"]


@pytest.mark.parametrize("required", [True, False])
def test_missing_due_source_never_asks_model_to_invent_it(
    deps: object, core_bank: object, rw_store: object,
    monkeypatch: pytest.MonkeyPatch, required: bool,
) -> None:
    init_merchant_schema(rw_store)
    core_bank.book_transfer(
        user_id=deps.user_id, account_id="acc_savings", payee_id="p_lina",
        amount=Money(10000), booked_on=date(2026, 10, 1), remark="",
        idempotency_key="node-context-missing",
    )
    original = PendingHandler.build

    def lost_source(self: PendingHandler, intents: object, gateway: FactGateway) -> object:
        plan = original(self, intents, gateway)
        monkeypatch.setattr(FactGateway, "scheduled_transfers_of", lambda *_a, **_kw: ())
        return plan

    monkeypatch.setattr(PendingHandler, "build", lost_source)
    reviewer = CapturingReviewer()
    view = Session(replace(deps, reviewer=reviewer, reviewer_required=required)).check_due()
    assert view.state is (SessionState.TAKEN_OVER if required else SessionState.AWAITING_CONFIRM)
    assert reviewer.requests == []


def test_regular_node_keeps_accepted_intents_and_only_referenced_facts(deps: object) -> None:
    reviewer = CapturingReviewer()
    view = Session(replace(deps, reviewer=reviewer)).submit(
        "转给李娜 500 元", selected={"account": "acc_savings"},
    )
    assert view.state is SessionState.AWAITING_CONFIRM
    request = reviewer.requests[0]
    context = request.slot_sources["node_context"][request.plan.nodes[0].node_id]
    assert context["proposal_basis"] == {
        "source": "accepted_intents", "candidate_intent_indices": [0],
    }
    assert set(context["fact_refs"]) == {"accounts:acc_savings", "payees:p_lina"}
    assert request.slot_sources["intents"][0]["selected_sources"]["account"] == "user_selection"


@pytest.mark.parametrize("identity_confirmed, blocked", [(False, False), (True, True)])
def test_known_counterparty_state_remains_a_reviewable_fact(
    deps: object, core_bank: object, rw_store: object,
    identity_confirmed: bool, blocked: bool,
) -> None:
    init_merchant_schema(rw_store)
    core_bank.book_transfer(
        user_id=deps.user_id, account_id="acc_savings", payee_id="p_lina",
        amount=Money(10000), booked_on=date(2026, 10, 1), remark="",
        idempotency_key="node-context-known-state",
    )
    with rw_store.transaction() as conn:
        conn.execute(
            "UPDATE payees SET identity_confirmed = ?, blocked = ? WHERE payee_id = ?",
            (int(identity_confirmed), int(blocked), "p_lina"),
        )
    reviewer = CapturingReviewer()
    view = Session(replace(deps, reviewer=reviewer, reviewer_required=True)).check_due()
    assert len(reviewer.requests) == 1
    request = reviewer.requests[0]
    payee = next(
        fact for fact in request.slot_sources["facts"] if fact["record_id"] == "payees:p_lina"
    )
    assert payee["identity_confirmed"] is identity_confirmed
    assert payee["blocked"] is blocked
    assert all(
        context["missing_sources"] == []
        for context in request.slot_sources["node_context"].values()
    )
    if blocked:
        assert view.decision.level is Level.DENY


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("missing_object", ["account", "payee"])
def test_object_fact_lost_after_planning_preserves_review_enforcement(
    deps: object, core_bank: object, rw_store: object,
    monkeypatch: pytest.MonkeyPatch, required: bool, missing_object: str,
) -> None:
    init_merchant_schema(rw_store)
    core_bank.book_transfer(
        user_id=deps.user_id, account_id="acc_savings", payee_id="p_lina",
        amount=Money(10000), booked_on=date(2026, 10, 1), remark="",
        idempotency_key="node-context-lost-object",
    )
    before = tuple(tuple(row) for row in rw_store.query("SELECT * FROM accounts"))
    original = PendingHandler.build

    def lost_object(self: PendingHandler, intents: object, gateway: FactGateway) -> object:
        plan = original(self, intents, gateway)
        method = "accounts_of" if missing_object == "account" else "payees_of"
        monkeypatch.setattr(FactGateway, method, lambda *_a, **_kw: ())
        return plan

    monkeypatch.setattr(PendingHandler, "build", lost_object)
    reviewer = CapturingReviewer()
    view = Session(replace(deps, reviewer=reviewer, reviewer_required=required)).check_due()
    assert view.state is (SessionState.TAKEN_OVER if required else SessionState.AWAITING_CONFIRM)
    assert reviewer.requests == []
    assert tuple(tuple(row) for row in rw_store.query("SELECT * FROM accounts")) == before
    assert not rw_store.query("SELECT * FROM transactions WHERE channel = 'agent'")


@pytest.mark.parametrize("required", [True, False])
def test_birthday_profile_lost_after_planning_never_becomes_model_authority(
    deps: object, rw_store: object, monkeypatch: pytest.MonkeyPatch, required: bool,
) -> None:
    init_transfer_schema(rw_store)
    init_merchant_schema(rw_store)
    seed_user_profiles(rw_store)
    original = PendingHandler.build

    def lost_profile(self: PendingHandler, intents: object, gateway: FactGateway) -> object:
        plan = original(self, intents, gateway)
        assert any(node.action == "merchant.order" for node in plan.nodes)
        monkeypatch.setattr(FactGateway, "user_profile", lambda *_a, **_kw: None)
        return plan

    monkeypatch.setattr(PendingHandler, "build", lost_profile)
    reviewer = CapturingReviewer()
    view = Session(replace(deps, reviewer=reviewer, reviewer_required=required)).check_due()
    assert view.state is (SessionState.TAKEN_OVER if required else SessionState.AWAITING_CONFIRM)
    assert reviewer.requests == []
    assert not rw_store.query("SELECT * FROM merchant_orders")


def test_missing_review_fact_does_not_block_a_pure_query(
    deps: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = BillsHandler.build

    def lost_account(self: BillsHandler, intents: object, gateway: FactGateway) -> object:
        plan = original(self, intents, gateway)
        monkeypatch.setattr(FactGateway, "accounts_of", lambda *_a, **_kw: ())
        return plan

    monkeypatch.setattr(BillsHandler, "build", lost_account)
    reviewer = CapturingReviewer()
    view = Session(replace(deps, reviewer=reviewer, reviewer_required=True)).submit(
        "查余额", selected={"account": "acc_savings"},
    )
    assert view.state is SessionState.AWAITING_CONFIRM
    assert view.card.level is Level.GREEN
    assert reviewer.requests == []


@pytest.mark.parametrize(
    "utterance, selected, expected_refs",
    [
        ("发起收款 100 元", {"payer": "p_lina"}, {"payees:p_lina"}),
        (
            "申购 100 元", {"account": "acc_savings", "product": "wp_cash"},
            {"accounts:acc_savings", "wealth_products:wp_cash"},
        ),
    ],
)
def test_node_references_use_gateway_record_names(
    deps: object, utterance: str, selected: dict[str, str], expected_refs: set[str],
) -> None:
    reviewer = CapturingReviewer()
    Session(replace(deps, reviewer=reviewer)).submit(utterance, selected=selected)
    request = reviewer.requests[0]
    facts = {fact["record_id"] for fact in request.slot_sources["facts"]}
    context = request.slot_sources["node_context"][request.plan.nodes[0].node_id]
    assert set(context["fact_refs"]) == expected_refs
    assert expected_refs <= facts
    assert context["missing_sources"] == []
