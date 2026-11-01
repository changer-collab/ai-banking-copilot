from collections.abc import Mapping
from datetime import date

import pytest

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.kernel import Kernel
from ai_banking_copilot.planning.llm import Intent, IntentProposal
from ai_banking_copilot.planning.slots import SlotInputs, fill_control_slots
from ai_banking_copilot.tools.catalog import build_default_registry

TODAY = date(2026, 10, 1)


class _BalanceModel:
    def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
        # 故意裁掉对象限定；范围校验必须回看完整请求，不能相信模型片段。
        return IntentProposal(
            intents=(Intent("account.balance", {"span": "余额"}, 1.0, "余额"),),
            raw=utterance,
        )


@pytest.mark.parametrize(
    ("action", "text"),
    [
        ("account.balance", "请核对远川银行账户的余额"),
        ("account.balance", "我想看看她的账户余额"),
        ("account.balance", "查询李娜的余额"),
        ("account.balance", "看工行余额"),
        ("account.balance", "查李娜余额"),
        ("account.balance", "看看余额，要工行"),
        ("account.balance", "看看余额，用李娜"),
        ("account.balance", "看看余额，是工行"),
        ("account.balance", "看看余额，我要工行"),
        ("account.balance", "看看余额，是李娜的那张银行卡"),
        ("account.balance", "他的卡里还剩多少钱"),
        ("account.balance", "看看余额，要临海银行账户的"),
        ("account.transactions", "查询远川银行的交易流水"),
        ("account.transactions", "查看他的交易记录"),
        ("account.transactions", "查工行流水"),
        ("account.transactions", "看李娜交易记录"),
        ("account.transactions", "把交易明细给我看看，要李娜账户的"),
    ],
)
def test_control_slots_do_not_replace_out_of_scope_subject_with_selected_self(
    action: str, text: str, payees: tuple[Payee, ...],
) -> None:
    fill = fill_control_slots(
        action,
        SlotInputs(text=text, today=TODAY, payees=payees, selected={"account": "acc_savings"}),
    )
    assert not fill.complete
    assert "account" not in fill.slots
    assert any(question.slot == "account" for question in fill.questions)


@pytest.mark.parametrize(
    ("action", "text"),
    [
        ("account.balance", "帮我看看账户余额"),
        ("account.balance", "请核对我银行账户的余额"),
        ("account.balance", "查看我在银行的余额"),
        ("account.balance", "查询余额是多少"),
        ("account.balance", "给我看下余额"),
        ("account.balance", "帮我查下余额"),
        ("account.balance", "想看看余额"),
        ("account.balance", "看看余额，是否够用"),
        ("account.balance", "请查看我名下账户的余额"),
        ("account.transactions", "查我的银行账户流水"),
        ("account.transactions", "就当前选中的那个账户，流水给我过一下"),
        ("account.transactions", "有没有异常交易"),
        ("account.transactions", "把交易明细给我看看"),
    ],
)
def test_control_slots_keep_ordinary_self_queries_without_extra_clarification(
    action: str, text: str,
) -> None:
    fill = fill_control_slots(
        action,
        SlotInputs(text=text, today=TODAY, selected={"account": "acc_current"}),
    )
    assert fill.complete
    assert fill.questions == ()
    assert fill.slots["account"] == "acc_current"


def _context(gateway: FactGateway) -> dict[str, object]:
    return {
        "today": TODAY,
        "selected": {"user_id": DEMO_USER_ID, "account": "acc_savings"},
        "payees": tuple(record.payload for record in gateway.payees_of(DEMO_USER_ID)),
    }


def test_kernel_cropped_model_span_cannot_drop_external_account_scope(gateway: FactGateway) -> None:
    result = Kernel(_BalanceModel(), build_default_registry()).plan(
        DEMO_USER_ID, "请查询远川银行储蓄账户的余额", _context(gateway),
    )
    assert result.blocked
    assert result.plan.nodes == ()
    assert any(question.slot == "account" for question in result.questions)


def test_kernel_query_scope_ignores_payee_in_another_clause(gateway: FactGateway) -> None:
    class _TwoActionModel:
        def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
            return IntentProposal(
                intents=(
                    Intent("transfer.create", {"span": "转给李娜20元"}, 1.0, "转账"),
                    Intent("account.balance", {"span": "余额"}, 1.0, "余额"),
                ),
                raw=utterance,
            )

    result = Kernel(_TwoActionModel(), build_default_registry()).plan(
        DEMO_USER_ID, "转给李娜20元，然后查看当前账户余额", _context(gateway),
    )
    assert result.questions == ()
    assert [node.action for node in result.plan.nodes] == ["transfer.create", "account.balance"]
    assert result.plan.nodes[1].params["account_id"] == "acc_savings"


@pytest.mark.parametrize(
    "text",
    [
        "列出活期账户本月金额超过两百元的消费",
        "统计储蓄账户这个月超过 200 元的支出",
        "列出我名下账户本月金额超过两百元的消费",
    ],
)
def test_amount_and_time_predicate_is_not_treated_as_foreign_object(text: str) -> None:
    """谓词不是对象：金额 / 时间条件剥离后只剩「活期账户」这类自称词，不得追问账户。"""
    fill = fill_control_slots(
        "account.transactions",
        SlotInputs(text=text, today=TODAY, selected={"account": "acc_current"}),
    )
    assert fill.questions == ()
    assert fill.slots["account"] == "acc_current"


@pytest.mark.parametrize(
    ("text", "account_id"),
    [
        ("请查我的储蓄账户余额", "acc_savings"),
        ("请查我的活期账户余额", "acc_current"),
        ("照我选的这个账户，余额还剩多少？", "acc_current"),
        ("我能先看一下自己的余额吗？", "acc_savings"),
    ],
)
def test_kernel_keeps_trusted_owned_account_selection_for_self_account_types(
    gateway: FactGateway, text: str, account_id: str,
) -> None:
    context = _context(gateway)
    context["selected"] = {"account": account_id}
    result = Kernel(_BalanceModel(), build_default_registry()).plan(
        DEMO_USER_ID, text, context,
    )
    assert not result.blocked
    assert result.questions == ()
    assert len(result.plan.nodes) == 1
    assert result.plan.nodes[0].params["account_id"] == account_id


@pytest.mark.parametrize(
    ("text", "transfer_span", "has_account_question"),
    [
        ("先给李娜转账20元，然后看看余额，是他的那张卡", "给李娜转账20元", True),
        ("先给李娜转账20元，然后他的那张卡，查余额", "给李娜转账20元", True),
        ("看看余额，然后给李娜的账户转账20元", "给李娜的账户转账20元", False),
    ],
)
def test_kernel_query_scope_keeps_unassigned_suffix_until_next_intent(
    gateway: FactGateway, text: str, transfer_span: str, has_account_question: bool,
) -> None:
    class _TwoActionModel:
        def propose(self, utterance: str, context: Mapping[str, object]) -> IntentProposal:
            intents = (
                Intent("transfer.create", {"span": transfer_span}, 1.0, "转账"),
                Intent("account.balance", {"span": "余额"}, 1.0, "余额"),
            )
            return IntentProposal(
                intents=tuple(sorted(
                    intents, key=lambda intent: utterance.index(str(intent.slots["span"])),
                )),
                raw=utterance,
            )

    result = Kernel(_TwoActionModel(), build_default_registry()).plan(
        DEMO_USER_ID, text, _context(gateway),
    )
    assert any(question.slot == "account" for question in result.questions) == has_account_question
    has_balance = any(node.action == "account.balance" for node in result.plan.nodes)
    assert has_balance != has_account_question
    assert any(node.action == "transfer.create" for node in result.plan.nodes)
