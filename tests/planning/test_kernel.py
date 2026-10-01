from datetime import date

from ai_banking_copilot.contracts.plan import AnchorSource, cfhash, validate_plan
from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.planning.kernel import Kernel, PlanningResult
from ai_banking_copilot.planning.llm import TemplateIntentModel

TODAY = date(2026, 10, 1)


def _context(
    payees: tuple[Payee, ...], selected: dict[str, object] | None = None
) -> dict[str, object]:
    default_selected: dict[str, object] = {"user_id": "u_demo", "account": "acc_savings"}
    return {
        "today": TODAY,
        "payees": payees,
        "selected": default_selected if selected is None else selected,
    }


def _kernel(registry: object) -> Kernel:
    return Kernel(TemplateIntentModel(), registry)  # type: ignore[arg-type]


def test_single_transfer_produces_one_valid_node(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    result = _kernel(stub_registry).plan("u_demo", "转给李娜 500 元", _context(payees))
    assert isinstance(result, PlanningResult)
    assert result.blocked is False
    assert result.questions == ()
    validate_plan(result.plan)
    node = result.plan.nodes[0]
    assert node.action == "transfer.create"
    assert node.params["user_id"] == "u_demo"
    assert node.params["payee_id"] == "p_lina"
    assert node.params["amount_cents"] == 50000
    assert node.params["account_id"] == "acc_savings"
    assert node.intent is not None
    assert node.intent.source is AnchorSource.EXPLICIT


def test_multi_intent_on_the_same_resource_is_serialised_into_a_dag(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    result = _kernel(stub_registry).plan(
        "u_demo", "转给李娜 500 元，转给张强 300 元", _context(payees)
    )
    validate_plan(result.plan)
    assert [node.node_id for node in result.plan.nodes] == ["n1", "n2"]
    assert result.plan.nodes[1].depends_on == ("n1",)
    assert "机械推导锚" in result.rationale


def test_query_node_does_not_chain_behind_other_nodes(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    result = _kernel(stub_registry).plan(
        "u_demo", "查一下上月账单，然后转给李娜 500 元", _context(payees)
    )
    validate_plan(result.plan)
    assert [node.action for node in result.plan.nodes] == ["bills.monthly", "transfer.create"]
    assert result.plan.nodes[1].depends_on == ()


def test_clarification_yields_questions_and_no_node(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    result = _kernel(stub_registry).plan("u_demo", "转给王伟 500 元", _context(payees))
    assert result.blocked is True
    assert [question.slot for question in result.questions] == ["payee"]
    assert result.plan.nodes == ()


def test_rationale_exposes_the_frozen_plan_hash(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    result = _kernel(stub_registry).plan("u_demo", "转给李娜 500 元", _context(payees))
    assert f"cfhash={cfhash(result.plan.nodes)}" in result.rationale


def test_unknown_utterance_produces_an_empty_plan(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    result = _kernel(stub_registry).plan("u_demo", "今天天气不错", _context(payees))
    assert result.plan.nodes == ()
    assert result.blocked is True
    assert result.intents == ()
