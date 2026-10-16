import pytest

from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.bills import BillsHandler
from ai_banking_copilot.scenarios.cross import CrossHandler


def _balance() -> Intent:
    return Intent("account.balance", slots={"account": Value("acc_savings", Trust.TRUSTED)})


def test_balance_query_uses_the_selected_account(gateway: FactGateway) -> None:
    plan = BillsHandler().build([_balance()], gateway)
    assert [(node.action, node.params) for node in plan.nodes] == [
        ("account.balance", {"account_id": "acc_savings"})
    ]


@pytest.mark.parametrize("action", ["bills.monthly", "bills.yearly", "account.transactions"])
def test_balance_is_preserved_with_other_queries(gateway: FactGateway, action: str) -> None:
    plan = BillsHandler().build([_balance(), Intent(action)], gateway)
    actions = [node.action for node in plan.nodes]
    assert "account.balance" in actions
    assert action in actions
    assert len({node.node_id for node in plan.nodes}) == len(plan.nodes)


def test_balance_is_preserved_in_a_cross_domain_plan(gateway: FactGateway) -> None:
    plan = CrossHandler().build([_balance(), Intent("card.list")], gateway)
    assert [node.action for node in plan.nodes] == ["account.balance", "card.list"]
