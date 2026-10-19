from collections.abc import Mapping
from dataclasses import replace

import pytest

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.planning.llm import Intent, IntentProposal
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session, SessionState


class _AccountQueryModel:
    def __init__(self, span: str | None = None, action: str = "account.balance") -> None:
        self._span = span
        self._action = action

    def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
        return IntentProposal(
            intents=(Intent(self._action, {"span": self._span or utterance}, 1.0, "账户查询"),),
            raw=utterance,
        )


@pytest.mark.parametrize(
    ("text", "span"),
    [
        ("帮我看临海银行的账户余额", None),
        ("查看他的账户余额", None),
        ("查张强的余额", None),
        ("查看余额宝的余额", None),
        ("查余额宝余额", None),
        ("麻烦查询星湖银行的余额", "余额"),
    ],
)
def test_balance_plan_requires_subject_in_trusted_account_scope(
    deps: AppDeps, text: str, span: str | None,
) -> None:
    session = Session(replace(deps, intent_model=_AccountQueryModel(span)))
    view = session.submit(text, selected={"account": "acc_savings"})
    assert view.state is SessionState.NEEDS_CLARIFICATION
    assert view.plan is None
    assert view.card is None
    assert view.decision is None
    assert any(question.slot == "account" for question in view.clarifications)


@pytest.mark.parametrize(
    ("text", "selected", "account_id"),
    [
        ("查余额", None, "acc_savings"),
        ("帮我核对当前账户余额", {"account": "acc_current"}, "acc_current"),
        ("看看我的银行账户余额", {"account": "acc_savings"}, "acc_savings"),
        ("请核对我的储蓄账户余额", None, "acc_savings"),
        ("请核对我的活期账户余额", {"account": "acc_current"}, "acc_current"),
        (
            "查我的余额，备注：对账说明提到临海银行和他的账户",
            {"account": "acc_savings"},
            "acc_savings",
        ),
    ],
)
def test_self_balance_queries_keep_defaults_selection_and_data_only_remarks(
    deps: AppDeps, text: str, selected: dict[str, object] | None, account_id: str,
) -> None:
    view = Session(replace(deps, intent_model=_AccountQueryModel())).submit(text, selected=selected)
    assert view.state is SessionState.AWAITING_CONFIRM
    assert view.clarifications == ()
    assert view.plan is not None
    assert len(view.plan.nodes) == 1
    assert view.plan.nodes[0].params["account_id"] == account_id
    assert view.card is not None and view.card.level is Level.GREEN


def test_payee_in_another_action_does_not_change_balance_query_subject(deps: AppDeps) -> None:
    class _TransferAndBalanceModel:
        def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
            return IntentProposal(
                intents=(
                    Intent("transfer.create", {"span": "给李娜转账20元"}, 1.0, "转账"),
                    Intent("account.balance", {"span": "查看当前账户余额"}, 1.0, "余额"),
                ),
                raw=utterance,
            )

    view = Session(replace(deps, intent_model=_TransferAndBalanceModel())).submit(
        "先给李娜转账20元，然后查看当前账户余额", selected={"account": "acc_savings"},
    )
    assert view.state is SessionState.AWAITING_CONFIRM
    assert view.clarifications == ()
    assert view.plan is not None
    assert [node.action for node in view.plan.nodes] == ["transfer.create", "account.balance"]
    assert view.plan.nodes[1].params["account_id"] == "acc_savings"


def test_account_reference_requires_explicit_selection_and_can_be_clarified(deps: AppDeps) -> None:
    session = Session(replace(deps, intent_model=_AccountQueryModel()))
    ambiguous = session.submit("请查这个账户余额")
    assert ambiguous.state is SessionState.NEEDS_CLARIFICATION
    assert ambiguous.plan is None
    assert any(question.slot == "account" for question in ambiguous.clarifications)

    clarified = session.clarify({"account": "acc_current"})
    assert clarified.state is SessionState.AWAITING_CONFIRM
    assert clarified.plan is not None
    assert clarified.plan.nodes[0].params["account_id"] == "acc_current"


@pytest.mark.parametrize(
    "text", ["工行账户的流水，近几笔给我看看", "把支付宝最近的流水摊开一下"],
)
def test_unresolved_transactions_scope_does_not_emit_plan(deps: AppDeps, text: str) -> None:
    model = _AccountQueryModel("流水", "account.transactions")
    view = Session(replace(deps, intent_model=model)).submit(
        text, selected={"account": "acc_savings"},
    )
    assert view.state is SessionState.NEEDS_CLARIFICATION
    assert view.plan is None
    assert view.card is None
    assert any(question.slot == "account" for question in view.clarifications)


@pytest.mark.parametrize("action", ["account.balance", "account.transactions"])
@pytest.mark.parametrize(
    "text",
    [
        "先给李娜转账20元，然后看看{query}，是他的那张卡",
        "先给李娜转账20元，然后他的那张卡，查{query}",
    ],
)
def test_query_object_is_retained_in_multi_intent(deps: AppDeps, action: str, text: str) -> None:
    query = "余额" if action == "account.balance" else "流水"

    class _TransferAndQueryModel:
        def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
            return IntentProposal(
                intents=(
                    Intent("transfer.create", {"span": "给李娜转账20元"}, 1.0, "转账"),
                    Intent(action, {"span": query}, 1.0, "查询"),
                ),
                raw=utterance,
            )

    view = Session(replace(deps, intent_model=_TransferAndQueryModel())).submit(
        text.format(query=query), selected={"account": "acc_savings"},
    )
    assert view.state is SessionState.NEEDS_CLARIFICATION
    assert view.plan is None
    assert any(question.slot == "account" for question in view.clarifications)


def test_following_transfer_object_does_not_change_query_scope(deps: AppDeps) -> None:
    class _QueryAndTransferModel:
        def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
            return IntentProposal(
                intents=(
                    Intent("account.balance", {"span": "查看当前账户余额"}, 1.0, "余额"),
                    Intent("transfer.create", {"span": "给李娜转账20元"}, 1.0, "转账"),
                ),
                raw=utterance,
            )

    view = Session(replace(deps, intent_model=_QueryAndTransferModel())).submit(
        "先查看当前账户余额，然后给李娜转账20元", selected={"account": "acc_savings"},
    )
    assert view.state is SessionState.AWAITING_CONFIRM
    assert view.clarifications == ()
    assert view.plan is not None
    balance = next(node for node in view.plan.nodes if node.action == "account.balance")
    assert balance.params["account_id"] == "acc_savings"
