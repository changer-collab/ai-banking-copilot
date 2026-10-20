"""账单出处接线：捕获器固定放行，不代表真实模型审查效果。"""

from dataclasses import replace
from datetime import date

import pytest

from ai_banking_copilot.contracts.taint import Slot, SlotKind, Trust, Value
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.planning.llm import Intent, IntentProposal
from ai_banking_copilot.planning.reviewer import ReviewReport, ReviewStatus
from ai_banking_copilot.scenarios.bills import BillsHandler
from ai_banking_copilot.web.session import Session, SessionState


class Capture:
    def __init__(self):
        self.requests = []

    def review(self, request):
        self.requests.append(request)
        return ReviewReport(
            execution_id=request.execution_id, cfhash=request.cfhash,
            prompt_version=request.prompt_version, status=ReviewStatus.ALLOW,
        )


class Fixed:
    def __init__(self, action, slots=None):
        self.action, self.slots = action, slots or {}

    def propose(self, utterance, context):
        return IntentProposal((Intent(self.action, self.slots, 1.0),), utterance)


def capture(deps, action="bills.monthly", selected=None, model_slots=None):
    reviewer = Capture()
    session = Session(replace(
        deps, reviewer=reviewer, intent_model=Fixed(action, model_slots),
    ))
    view = session.submit("查看账单", selected=selected)
    assert view.state is SessionState.AWAITING_CONFIRM
    return reviewer.requests[0], session


def test_monthly_default_uses_booking_clock_and_submit_is_read_only(deps, rw_store):
    before = {
        table: tuple(tuple(row) for row in rw_store.query(f"SELECT * FROM {table}"))
        for table in ("accounts", "transactions")
    }
    request, _ = capture(replace(deps, today=date(2030, 2, 3)))
    assert [node.node_id for node in request.plan.nodes] == ["n1"]
    context = request.slot_sources["node_context"]["n1"]
    assert context["intent_scope"] == "user_request"
    assert context["authorization"] == "pending_confirmation"
    basis = context["proposal_basis"]
    assert basis["period"] == {
        "year": 2026, "month": 9, "source": "server_rule",
        "rule": "previous_complete_month",
        "reference": "gateway.core_bank.DEFAULT_BOOKING_DATE",
        "reference_date": DEFAULT_BOOKING_DATE.isoformat(),
    }
    assert basis["user_id"] == {"value": deps.user_id, "source": "server_identity"}
    assert basis["candidate_intent_indices"] == [0]
    record = deps.gateway.monthly_bill(deps.user_id, 2026, 9)
    fact = next(f for f in request.slot_sources["facts"] if f["record_id"] == record.record_id)
    assert fact["content_hash"] == record.content_hash
    assert fact["aggregation"] == "transactions.user_id + booked_on month; category net totals"
    assert context["fact_refs"] == [record.record_id]
    for table, rows in before.items():
        assert tuple(tuple(row) for row in rw_store.query(f"SELECT * FROM {table}")) == rows


def test_selected_period_and_category_remain_accepted_sources(deps):
    request, session = capture(deps, "bills.category_trend", {
        "year": Value(2026, Trust.TRUSTED), "month": 9,
        "category": Slot(SlotKind.CONTROL, Value("餐饮", Trust.TRUSTED)),
    })
    intent = request.slot_sources["intents"][0]
    assert intent["slots"]["year"] == 2026
    assert intent["slots"]["category"] == "餐饮"
    assert intent["selected_sources"]["category"] == "user_selection"
    basis = request.slot_sources["node_context"]["n2"]["proposal_basis"]
    assert basis["period"]["selected_sources"] == {
        "year": "user_selection", "month": "user_selection",
    }
    assert basis["category"]["source"] == "user_selection"
    session.clarify({"category": "购物"})
    intent = session._deps.reviewer.requests[-1].slot_sources["intents"][0]
    assert intent["slots"]["category"] == "购物"
    assert intent["selected_sources"]["category"] == "clarification_answer"


def test_combined_monthly_and_trend_keep_real_intent_candidates(deps):
    reviewer = Capture()
    view = Session(replace(deps, reviewer=reviewer)).submit("查看账单，再查看分类趋势")
    assert view.state is SessionState.AWAITING_CONFIRM
    request = reviewer.requests[0]
    contexts = request.slot_sources["node_context"]
    assert contexts["n1"]["proposal_basis"]["candidate_intent_indices"] == [0]
    assert contexts["n1"]["proposal_basis"]["source"] == "accepted_intents"
    assert "required_by" not in contexts["n1"]["proposal_basis"]
    assert contexts["n2"]["proposal_basis"]["candidate_intent_indices"] == [1]


def test_trend_dependency_and_gateway_category_have_honest_sources(deps):
    request, _ = capture(deps, "bills.category_trend")
    contexts = request.slot_sources["node_context"]
    monthly = contexts["n1"]["proposal_basis"]
    assert monthly["source"] == "server_rule"
    assert monthly["required_by"] == ["n2"]
    assert monthly["candidate_intent_indices"] == []
    basis = contexts["n2"]["proposal_basis"]
    record = deps.gateway.monthly_bill(deps.user_id, 2026, 9)
    top = max(
        (item for item in record.payload.by_category if item.total.cents < 0),
        key=lambda item: (-item.total.cents, item.category),
    )
    assert basis["category"] == {
        "value": top.category, "source": "server_rule", "fact_ref": record.record_id,
        "rule": "largest_negative_category_net; ties use max(category)",
    }
    assert basis["base_period"] == {
        "year": 2026, "month": 8, "source": "server_rule", "rule": "previous_month_of_period",
    }


def test_mutated_plan_cannot_attest_period_category_or_identity(deps, monkeypatch):
    original = BillsHandler.build

    def changed(self, intents, gateway):
        plan = original(self, intents, gateway)
        return replace(plan, nodes=tuple(
            replace(node, params={
                **node.params, "user_id": "forged", "year": 2001, "month": 1,
                **({"category": "forged", "base_year": 2000} if node.node_id == "n2" else {}),
            }) for node in plan.nodes
        ))

    monkeypatch.setattr(BillsHandler, "build", changed)
    request, _ = capture(deps, "bills.category_trend")
    assert request.plan.nodes[0].params["year"] == 2001
    context = request.slot_sources["node_context"]["n2"]
    assert context["proposal_basis"]["period"]["year"] == 2026
    assert context["proposal_basis"]["category"]["value"] != "forged"
    assert context["proposal_basis"]["user_id"]["value"] == deps.user_id
    assert all(f.get("user_id") != "forged" for f in request.slot_sources["facts"])


def test_unaccepted_model_slots_never_become_evidence(deps):
    request, _ = capture(deps, "bills.category_trend", model_slots={
        "year": 2001, "month": 1, "category": "forged",
    })
    assert "year" not in request.slot_sources["intents"][0]["slots"]
    basis = request.slot_sources["node_context"]["n2"]["proposal_basis"]
    assert basis["period"]["year"] == 2026
    assert basis["category"]["value"] != "forged"


@pytest.mark.parametrize("text", ["查看分类趋势", "查询账单分类趋势"])
def test_explicit_template_trend_reaches_reviewer(deps, text):
    reviewer = Capture()
    view = Session(replace(deps, reviewer=reviewer)).submit(text)
    assert view.state is SessionState.AWAITING_CONFIRM
    assert reviewer.requests[0].candidate_actions == ("bills.category_trend",)
