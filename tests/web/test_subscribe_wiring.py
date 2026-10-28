"""Web 链申购接线：受信任注入风险测评后出卡；赎回不漂移；缺测评 fail-closed。"""

from __future__ import annotations

from dataclasses import replace

from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session


def test_subscribe_with_valid_assessment_emits_card(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("申购 2000 元理财", selected={"account": "acc_savings"})
    assert view.card is not None
    assert view.decision is not None
    assert view.decision.level.name == "RED"
    assert view.card.lines


def test_redeem_still_needs_product_clarification(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("赎回 500 元", selected={"account": "acc_savings"})
    assert view.card is None


def test_subscribe_without_risk_source_is_refused(deps: AppDeps) -> None:
    session = Session(replace(deps, risk=None))
    view = session.submit("申购 2000 元理财", selected={"account": "acc_savings"})
    assert view.card is None


def test_missing_product_asks_for_product_selection(deps: AppDeps) -> None:
    """缺产品时给出「请选择理财产品」的结构化提问：页面据此渲染下拉，不是自由文本死路。"""
    view = Session(deps).submit("赎回 500 元", selected={"account": "acc_savings"})
    assert view.card is None
    assert [ask.slot for ask in view.clarifications] == ["product"]


def test_trusted_product_selection_is_honoured(deps: AppDeps) -> None:
    """产品槽只认受信任选择：页面选中 wp_bond 后，卡片不得再回落默认 wp_cash。"""
    view = Session(deps).submit(
        "申购 2000 元", selected={"account": "acc_savings", "product": "wp_bond"}
    )
    assert view.card is not None
    text = "\n".join(view.card.lines)
    assert "稳利债券 A" in text
    assert "活期理财篮子" not in text
