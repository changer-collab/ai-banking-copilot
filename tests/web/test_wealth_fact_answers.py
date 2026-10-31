"""理财事实直答与确定性兜底（实测 WEA-Q18 / Q20 / Q03）。

这三条都要求「给结论 / 给事实，不要一句没太理解」；同时不能借机放宽任何闸门。
"""

from __future__ import annotations

from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session, SessionState


def test_holdings_question_ends_with_a_reconciliation_conclusion(deps: AppDeps) -> None:
    view = Session(deps).submit("持仓对不上")

    assert view.state is SessionState.IDLE
    assert "对账结论" in view.message
    assert "合计" in view.message
    assert "一致" in view.message
    assert view.card is None, "对账只回答事实，不产生任何计划"


def test_idle_cash_question_lists_product_facts_instead_of_guessing(deps: AppDeps) -> None:
    view = Session(deps).submit("我有5万块闲钱，半年内不用，放什么合适")

    assert view.state is SessionState.IDLE
    assert "只列事实" in view.message
    assert "期限" in view.message
    assert "没太理解" not in view.message
    assert "不预测" in view.message


def test_selection_question_is_stable_and_never_widens_the_level(deps: AppDeps) -> None:
    """服务端记录 R3，用户自述 R2：按更保守的 R2 筛，且同句多次一致。"""
    answers = {
        Session(deps).submit("我测评是R2，推荐几个适合我的").message for _ in range(3)
    }

    assert len(answers) == 1, "同一句连跑 3 次结论必须一致"
    message = answers.pop()
    assert "按你的测评等级 R2" in message
    assert "更保守" in message
    assert "成长精选" not in message, "R4 权益类不能出现在 R2 的候选里"


def test_claiming_a_higher_level_does_not_widen_the_eligible_set(deps: AppDeps) -> None:
    view = Session(deps).submit("我测评是R5，推荐几个适合我的")

    assert "按你的测评等级 R3" in view.message
    assert "不能按自述放宽" in view.message
    assert "成长精选" not in view.message


def test_anaphora_after_a_refused_subscribe_only_clarifies(deps: AppDeps) -> None:
    """WEA-Q05：上一轮因起购被拒，「就买刚才那个，500」不能替用户改金额。"""
    session = Session(deps)
    first = session.submit("申购 500 元稳利债券 A")
    assert "起购" in first.message

    second = session.submit("就买刚才那个，500")

    assert second.card is None, "被拒的上一轮不能自动承接成计划"
    assert second.plan is None
    assert "稳利债券 A" in second.message
    assert "不会替你改金额" in second.message
