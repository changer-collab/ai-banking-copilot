"""三分类接入：界面话术变了，但状态机、卡与熔断语义一律不变。"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from ai_banking_copilot.planning.llm import Intent, IntentProposal
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import (
    ClarificationAsk,
    Session,
    SessionState,
    SubmissionView,
    _amount_filter_verdict,
)


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


def test_amount_filter_query_is_explained_instead_of_planned() -> None:
    """账户流水不支持金额条件：宁可说明能力子集，也不能出一张忽略条件的查询卡。"""
    verdict = _amount_filter_verdict(
        "列出活期账户本月金额超过两百元的消费",
        (Intent("account.transactions", {}, 1.0, "消费"),),
    )
    assert verdict is not None
    assert verdict.kind == "ambiguous"
    assert "能力子集" in verdict.message
    assert verdict.suggestions


def test_amount_filter_verdict_does_not_touch_transfers() -> None:
    assert _amount_filter_verdict(
        "转给李娜 500 元", (Intent("transfer.create", {}, 1.0, "转账"),),
    ) is None


def test_intent_without_handler_falls_back_to_triage(deps: AppDeps) -> None:
    """模型给出没有处理器的动作时，仍要走三分类，不能回「未知意图」。"""

    class _UnknownActionModel:
        def propose(
            self, utterance: str, context: Mapping[str, object],
        ) -> IntentProposal:
            return IntentProposal(
                intents=(Intent("securities.trade", {}, 1.0, "炒股"),), raw=utterance,
            )

    from dataclasses import replace

    session = Session(replace(deps, intent_model=_UnknownActionModel()))
    view = session.submit("帮我炒股")
    assert view.state is SessionState.BLOCKED
    assert view.triage is not None and view.triage.kind == "out_of_scope"
    assert "证券" in view.message
    assert "未知意图" not in view.message


def test_clarify_does_not_loop_on_the_same_slot(
    deps: AppDeps, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一槽位刚回答过又被追问 ⇒ 停止循环并说明对象无法映射，不再无限澄清。"""
    ask = ClarificationAsk(slot="account", prompt="原文对象尚未匹配")
    repeated_view = SubmissionView(
        SessionState.NEEDS_CLARIFICATION, "信息不足", None, None, None, (),
        clarifications=(ask,),
    )
    session = Session(deps)
    session._last_utterance = "查工行账户的余额"  # noqa: SLF001
    monkeypatch.setattr(Session, "_submit", lambda *args, **kwargs: repeated_view)  # type: ignore[attr-defined]
    view = session.clarify({"account": "acc_current"})
    assert view.state is SessionState.BLOCKED
    assert view.triage is not None
    assert view.triage.kind == "ambiguous"
    assert "无法对应到本人账户" in view.message
