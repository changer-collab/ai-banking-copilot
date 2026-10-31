"""生活事件原型 L1–L7 + 三条组合律：七条各一条端到端 + 路由 + 反抢单。"""

from datetime import date
from pathlib import Path

import pytest

from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.plan import PlanNode
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.merchant_store import init_merchant_schema, seed_user_profiles
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.planning.llm import TemplateIntentModel
from ai_banking_copilot.policy.audit import AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.pep import Pep, _plan_hash
from ai_banking_copilot.policy.verification import (
    HmacStrongVerificationProofVerifier,
    VerificationBinding,
    create_challenge,
    verify_challenge,
)
from ai_banking_copilot.scenarios.base import (
    ScenarioRefused,
    ScenarioStatus,
    plan_is_pure_query,
    plan_net_effect,
)
from ai_banking_copilot.scenarios.life_events import (
    LIFE_EVENT_COMBOS,
    LIFE_EVENT_PROTOTYPES,
    build_life_event,
    build_life_event_combo,
    detect_life_event,
)
from ai_banking_copilot.scenarios.registry import (
    QUERY_HANDLERS,
    WRITE_HANDLERS,
    run_scenario,
    select_handler,
)
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.tools.registry import ToolCall

_TEST_KEY = b"life-events-test-key"


def _actions(plan) -> list[str]:
    return [node.action for node in plan.nodes]


def _anchors(plan) -> list[str]:
    return [node.intent.anchor_id for node in plan.nodes if node.intent is not None]


def test_prototype_registry_is_complete() -> None:
    prototype_ids = [item.event_id for item in LIFE_EVENT_PROTOTYPES]
    assert prototype_ids == ["L1", "L2", "L3", "L4", "L5", "L6", "L7"]
    assert len(LIFE_EVENT_COMBOS) == 3
    for combo in LIFE_EVENT_COMBOS:
        assert combo.first in prototype_ids
        assert combo.second in prototype_ids


def test_l1_budget_lock_settles_only_the_order(gateway: FactGateway) -> None:
    plan = build_life_event(
        "L1", {"budget": Money(100000), "order_cents": Money(29900)}, gateway
    )
    assert _actions(plan) == ["funds.freeze", "merchant.order", "funds.release"]
    release = plan.nodes[2]
    assert release.params["amount_cents"] == 70100
    assert release.depends_on == ("n1", "n2")
    assert plan_net_effect(plan, gateway).amount == Money(29900)


def test_l1_refuses_order_above_the_frozen_budget(gateway: FactGateway) -> None:
    with pytest.raises(ScenarioRefused):
        build_life_event("L1", {"budget": Money(100000), "order_cents": Money(200000)}, gateway)


def test_l2_schedules_monthly_contribution_then_lists_subscriptions(
    gateway: FactGateway,
) -> None:
    plan = build_life_event("L2", {"payee": "p_lina", "amount": Money(200000)}, gateway)
    assert _actions(plan) == ["transfer.schedule", "subscription.list"]
    schedule = plan.nodes[0]
    assert schedule.params["amount_cents"] == 200000
    assert schedule.params["currency"] == "CNY"
    assert schedule.params["booked_on"] == "2026-10-31"
    assert plan.nodes[1].depends_on == ("n1",)


def test_l3_tightens_then_restores_the_daily_limit(gateway: FactGateway) -> None:
    plan = build_life_event(
        "L3", {"card": "card_debit", "travel_limit": Money(500000)}, gateway
    )
    assert _actions(plan) == ["card.set_limit", "account.transactions", "card.set_limit"]
    assert plan.nodes[0].params["daily_limit_cents"] == 500000
    assert plan.nodes[-1].params["daily_limit_cents"] == 2000000
    assert plan.nodes[-1].depends_on == ("n1", "n2")


def test_l3_refuses_a_limit_that_does_not_tighten(gateway: FactGateway) -> None:
    with pytest.raises(ScenarioRefused):
        build_life_event("L3", {"card": "card_debit", "travel_limit": Money(2000000)}, gateway)


def test_l4_skips_redemption_when_the_balance_already_covers_it(gateway: FactGateway) -> None:
    plan = build_life_event(
        "L4",
        {"amount": Money(200000), "payee": "p_lina", "product": "wp_cash"},
        gateway,
    )
    assert _actions(plan) == ["account.balance", "transfer.create"]
    assert plan.nodes[1].depends_on == ("n1",)


def test_l4_redeems_exactly_the_gap(gateway: FactGateway) -> None:
    plan = build_life_event(
        "L4",
        {"amount": Money(20000000), "payee": "p_lina", "product": "wp_cash"},
        gateway,
    )
    assert _actions(plan) == ["account.balance", "wealth.redeem", "transfer.create"]
    assert plan.nodes[1].params["amount_cents"] == 20000000 - 12843000
    assert plan.nodes[2].depends_on == ("n2",)


def test_l5_reports_the_loss_and_issues_a_virtual_card(gateway: FactGateway) -> None:
    plan = build_life_event("L5", {"card": "card_debit"}, gateway)
    assert _actions(plan) == [
        "card.lock",
        "card.report_lost",
        "card.apply_virtual",
        "password.change",
        "account.transactions",
    ]
    assert plan_net_effect(plan, gateway).irreversibles == ("n2",)
    assert plan.nodes[2].params["daily_limit_cents"] == 2000000


def test_l6_is_a_pure_query_and_runs_green(gateway: FactGateway) -> None:
    plan = build_life_event("L6", {"year": 2026}, gateway)
    assert _actions(plan) == ["bills.yearly", "subscription.list"]
    assert plan_is_pure_query(plan)
    intent = TemplateIntentModel().propose("年终总结", {}).intents[0]
    outcome = run_scenario(select_handler([intent]), [intent], gateway)
    assert outcome.status is ScenarioStatus.PLANNED
    assert outcome.decision is not None
    assert outcome.decision.level is Level.GREEN


def test_l7_defaults_to_the_previous_billing_month(gateway: FactGateway) -> None:
    plan = build_life_event("L7", {"payer": "p_zhangqiang", "amount": Money(50000)}, gateway)
    assert _actions(plan) == [
        "transfer.request_collection",
        "account.transactions",
        "bills.monthly",
    ]
    billing = plan.nodes[-1].params
    assert (billing["year"], billing["month"]) == (2026, 9)


def test_combo_links_the_first_tail_to_the_second_root(gateway: FactGateway) -> None:
    plan = build_life_event_combo(
        "L6->L4",
        {
            "first": {"year": 2026},
            "second": {"amount": Money(20000000), "payee": "p_lina", "product": "wp_cash"},
        },
        gateway,
    )
    assert [node.node_id for node in plan.nodes] == ["a1", "a2", "b1", "b2", "b3"]
    assert plan.nodes[2].depends_on == ("a2",)
    assert plan.nodes[3].depends_on == ("b1",)
    anchors = _anchors(plan)
    assert len(set(anchors)) == len(anchors)


@pytest.mark.parametrize(
    ("utterance", "action", "event_id"),
    [
        ("我妈下个月过生日", "merchant.order", "L1"),
        ("我卡丢了", "card.lock", "L5"),
    ],
)
def test_spoken_life_events_route_to_the_prototype_handler(
    utterance: str, action: str, event_id: str
) -> None:
    intents = TemplateIntentModel().propose(utterance, {}).intents
    assert [intent.action for intent in intents] == [action]
    assert select_handler(intents).name == "life_events"
    assert detect_life_event(utterance) == event_id


def test_life_events_never_steal_the_single_domain_rules() -> None:
    for utterance, expected in (
        ("转给李娜 500 元", "transfer"),
        ("AA 收款 让李娜还我 500 元", "transfer"),
        ("查看年度账单", "bills"),
        ("把这张卡额度调到 5000", "cards"),
    ):
        intents = TemplateIntentModel().propose(utterance, {}).intents
        assert intents, utterance
        assert select_handler(intents).name == expected, utterance


def test_spoken_birthday_without_slots_still_plans_with_clarification_free_defaults(
    gateway: FactGateway,
) -> None:
    intent = TemplateIntentModel().propose("我妈下个月过生日", {}).intents[0]
    outcome = run_scenario(select_handler([intent]), [intent], gateway)
    assert outcome.status is ScenarioStatus.PLANNED
    assert outcome.plan is not None
    assert len(outcome.plan.nodes) == 3


def test_loss_report_without_a_card_asks_for_clarification(gateway: FactGateway) -> None:
    intent = TemplateIntentModel().propose("我卡丢了", {}).intents[0]
    outcome = run_scenario(select_handler([intent]), [intent], gateway)
    assert outcome.status is ScenarioStatus.NEEDS_CLARIFICATION
    assert outcome.decision is None


def test_build_life_event_rejects_unknown_prototype(gateway: FactGateway) -> None:
    with pytest.raises(ScenarioRefused):
        build_life_event("L9", {}, gateway)


def _call_of(node: PlanNode) -> ToolCall:
    """把计划节点还原成 `ToolCall`：控制参数标可信，日期按契约类型还原。"""
    params: dict[str, object] = {
        key: Value(date.fromisoformat(value) if key == "order_on" else value, Trust.TRUSTED)
        for key, value in node.params.items()
    }
    if node.action == "merchant.order":
        params["user_id"] = Value(DEMO_USER_ID, Trust.TRUSTED)
    return ToolCall(action=node.action, params=params)


def test_l1_plan_executes_end_to_end(tmp_path: Path) -> None:
    """冻结 1000 → 下单 299 → 解冻 701：可用余额只少 299，冻结记录终态 `released`。"""
    db = tmp_path / "life.db"
    writer = Store(db)
    init_schema(writer)
    seed_database(writer)
    init_merchant_schema(writer)
    seed_user_profiles(writer)
    writer.close()
    reader = Store(db, StoreMode.READ_ONLY)
    rw = Store(db, StoreMode.READ_WRITE)
    try:
        gateway = FactGateway(reader)
        plan = build_life_event(
            "L1", {"budget": Money(100000), "order_cents": Money(29900)}, gateway
        )
        before = gateway.account("acc_savings").payload.balance.cents
        pep = Pep(
            build_default_registry(),
            gateway,
            CoreBank(rw),
            AuditLog(tmp_path / "audit.jsonl"),
            CircuitBreaker(),
            WRITE_HANDLERS,
            QUERY_HANDLERS,
            strong_verification=HmacStrongVerificationProofVerifier(_TEST_KEY),
        )
        decision = Decision(level=Level.RED, reasons=(ReasonCode.AMOUNT_OVER_DAILY_LIMIT,))
        binding = VerificationBinding(
            user_id=DEMO_USER_ID,
            session_id="life-session",
            execution_id="life-execution",
            card_id="life-card",
            frozen_hash=_plan_hash(plan),
        )
        challenge, code = create_challenge(binding, key=_TEST_KEY)
        proof = HmacStrongVerificationProofVerifier(_TEST_KEY).issue(
            verify_challenge(challenge, binding, code, key=_TEST_KEY)
        )
        for node in plan.nodes:
            call = _call_of(node)
            result = pep.execute(
                call,
                decision,
                plan,
                f"life:{node.node_id}",
                execution=pep.issue_write_grant(
                    call,
                    decision,
                    plan,
                    user_id=DEMO_USER_ID,
                    session_id="life-session",
                    execution_id="life-execution",
                    approved_level=Level.RED,
                    verification_proof=proof,
                    card_id="life-card",
                ),
            )
            assert result.record_ids, node.node_id
        hold = gateway.hold(str(plan.nodes[0].params["budget_hold_id"])).payload
        assert hold.status == "released"
        assert hold.amount.cents == 70100
        assert gateway.account("acc_savings").payload.balance.cents == before - 29900
    finally:
        reader.close()
        rw.close()

