from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session


def test_multi_intent_utterance_yields_one_card_with_all_nodes(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit(
        "转给李娜 500 元，取消订阅",
        selected={"account": "acc_savings", "subscription": "sub_video"},
    )
    assert view.plan is not None
    assert [node.action for node in view.plan.nodes] == [
        "transfer.create",
        "subscription.cancel",
    ]
    assert view.card is not None


def test_session_defaults_grant_for_user_intents(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("冻结 500 元，转给李娜 300 元", selected={"account": "acc_savings"})
    assert view.plan is not None
    assert [node.action for node in view.plan.nodes] == ["funds.freeze", "transfer.create"]
