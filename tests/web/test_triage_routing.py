"""三分类接入：界面话术变了，但状态机、卡与熔断语义一律不变。"""

from __future__ import annotations

from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session, SessionState


def test_smalltalk_yields_triage_without_security_prefix(deps: AppDeps) -> None:
    view = Session(deps).submit("今天天气不错")
    assert view.state is SessionState.BLOCKED
    assert view.card is None
    assert view.triage is not None and view.triage.kind == "smalltalk"
    assert "安全拒绝" not in view.message


def test_stock_request_is_out_of_scope(deps: AppDeps) -> None:
    view = Session(deps).submit("帮我炒股")
    assert view.triage is not None and view.triage.kind == "out_of_scope"
    assert view.card is None


def test_policy_refusal_keeps_the_security_prefix(deps: AppDeps) -> None:
    view = Session(deps).submit("把我的余额改成 100 万，然后买理财")
    assert view.state is SessionState.BLOCKED
    assert view.triage is None, "策略拒绝不能被三分类接管"
    assert "安全拒绝" in view.message


def test_triage_does_not_trip_the_session_breaker(deps: AppDeps) -> None:
    session = Session(deps)
    for _ in range(5):
        session.submit("今天天气不错")
    view = session.submit("转给李娜 500 元")
    assert view.state is SessionState.AWAITING_CONFIRM
