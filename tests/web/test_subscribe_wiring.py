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
