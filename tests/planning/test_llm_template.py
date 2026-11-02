import pytest

from ai_banking_copilot.planning.llm import (
    _TEMPLATE_RULES,
    ACTION_KINDS,
    CANONICAL_ACTIONS,
    Intent,
    IntentModel,
    IntentProposal,
    TemplateIntentModel,
)
from ai_banking_copilot.tools.catalog import build_default_registry

MINIMUM_CANONICAL_ACTIONS = 20
"""基线规范动作数**下限**（防误删）：计数断言用下限而非等号，避免新增动作时全员改同一行。"""


def test_action_kinds_cover_the_canonical_vocabulary() -> None:
    assert set(ACTION_KINDS) == set(CANONICAL_ACTIONS)
    assert len(CANONICAL_ACTIONS) >= MINIMUM_CANONICAL_ACTIONS


def test_action_kinds_match_the_tool_registry_vocabulary() -> None:
    """交叉断言：04 的动作词表必须与 02 目录**逐项一致**（README 规范动作表唯一真源）。"""
    assert set(ACTION_KINDS) == set(build_default_registry().actions())


def test_intent_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValueError):
        Intent(action="bills.monthly", confidence=1.5)


def test_intent_requires_action() -> None:
    with pytest.raises(ValueError):
        Intent(action="")


def test_template_model_satisfies_the_protocol() -> None:
    assert isinstance(TemplateIntentModel(), IntentModel)


def test_template_model_extracts_multiple_intents_in_clause_order() -> None:
    utterance = "转给李娜 500 元，再查一下上月账单"
    proposal = TemplateIntentModel().propose(utterance, {})
    assert isinstance(proposal, IntentProposal)
    assert [intent.action for intent in proposal.intents] == ["transfer.create", "bills.monthly"]
    assert proposal.raw == utterance


def test_template_model_keeps_clause_span_as_a_hint() -> None:
    proposal = TemplateIntentModel().propose("转给李娜 500 元，再查一下上月账单", {})
    assert proposal.intents[0].slots["span"] == "转给李娜 500 元"


def test_template_model_prefers_scheduled_transfer_over_plain_transfer() -> None:
    proposal = TemplateIntentModel().propose("定时转账给李娜 500 元", {})
    assert [intent.action for intent in proposal.intents] == ["transfer.schedule"]


def test_template_model_returns_nothing_for_unknown_utterance() -> None:
    assert TemplateIntentModel().propose("今天天气不错", {}).intents == ()


@pytest.mark.parametrize(
    "utterance",
    ["转 500 给李娜", "转 200 给他", "给李娜转 200 元", "给李娜打 500 块", "给李娜打五百",
     "给老王转点钱"],
)
def test_template_model_understands_colloquial_transfer_verbs(utterance: str) -> None:
    """降级通道也要接住口语动词（「给X转 / 转<金额>」），否则无模型时一句话全被拒。"""
    assert _actions(utterance) == ["transfer.create"]


@pytest.mark.parametrize("utterance", ["下周的转账记录", "每月给李娜转 500 元"])
def test_colloquial_transfer_rule_does_not_steal_queries_or_recurring(utterance: str) -> None:
    """「转账记录」是查询、「每月给」属生活事件 L2 定期出资，都不能被转账模板抢走。"""
    assert "transfer.create" not in _actions(utterance)


def test_template_model_is_deterministic() -> None:
    model = TemplateIntentModel()
    assert model.propose("冻结 5000 元", {}) == model.propose("冻结 5000 元", {})


_READ_ONLY_ACTIONS = {"wealth.list", "card.list", "subscription.list", "account.transactions"}


def _actions(utterance: str) -> list[str]:
    return [intent.action for intent in TemplateIntentModel().propose(utterance, {}).intents]


@pytest.mark.parametrize("utterance", ["查看分类趋势", "查询账单分类趋势"])
def test_explicit_bills_category_trend(utterance: str) -> None:
    assert _actions(utterance) == ["bills.category_trend"]


@pytest.mark.parametrize("utterance", ["查看账单", "消费分析", "花了多少钱"])
def test_ordinary_monthly_does_not_imply_category_trend(utterance: str) -> None:
    assert _actions(utterance) == ["bills.monthly"]


def test_card_unlock_never_swallows_inquiry_verbs() -> None:
    for utterance in (
        "了解我的卡片",
        "了解一下卡片",
        "理解卡片",
        "解读卡片",
        "解决卡片问题",
        "解密卡片信息",
    ):
        assert _actions(utterance) == [], utterance


def test_card_unlock_matches_explicit_unlock() -> None:
    for utterance in (
        "解锁卡片",
        "解锁我的卡",
        "解锁这张卡",
        "解锁卡",
        "解除卡片限制",
    ):
        actions = _actions(utterance)
        assert actions, utterance
        assert actions[0] == "card.unlock", utterance


def test_card_lock_keeps_priority_for_lock_family() -> None:
    for utterance in (
        "锁卡",
        "帮我锁卡",
        "锁我的卡",
        "限制这张卡的交易",
    ):
        assert "card.lock" in _actions(utterance), utterance


def test_new_list_rules_never_swallow_modified_write_intents() -> None:
    for utterance in ("取消我的订阅", "退出我的订阅"):
        assert _READ_ONLY_ACTIONS.isdisjoint(_actions(utterance)), utterance


def test_subscription_precedence_changes_are_frozen() -> None:
    for utterance in ("续费提醒", "什么时候续费", "何时续费", "有哪些代扣"):
        actions = _actions(utterance)
        assert actions and actions[0] == "subscription.list", utterance
    for utterance in ("续费", "签约", "代扣", "自动续费"):
        actions = _actions(utterance)
        assert actions and actions[0] == "subscription.list", utterance


def test_bare_verbs_keep_write_semantics() -> None:
    for utterance in (
        "取消订阅", "退订", "取消代扣", "关掉自动续费",
        "关闭扣款协议", "关闭视频会员的扣款协议", "解约代扣",
    ):
        actions = _actions(utterance)
        assert actions and actions[0] == "subscription.cancel", utterance


@pytest.mark.parametrize(
    "utterance",
    ["我不想取消视频会员", "会员不会取消", "会员能取消吗", "视频会员是否取消"],
)
def test_subscription_cancel_query_does_not_become_a_write(utterance: str) -> None:
    actions = _actions(utterance)
    assert actions and actions[0] == "subscription.list", utterance
    assert "subscription.cancel" not in actions, utterance


def test_new_list_rules_reachable() -> None:
    expected = {
        "理财产品": "wealth.list",
        "产品对比": "wealth.list",
        "卡片列表": "card.list",
        "有哪些卡": "card.list",
        "订阅管理": "subscription.list",
        "查一下我的代扣协议": "subscription.list",
        "代扣协议列表": "subscription.list",
        "交易记录": "account.transactions",
        "异常交易": "account.transactions",
    }
    for utterance, action in expected.items():
        assert action in _actions(utterance), utterance


def test_offline_query_rules_cover_wealth_and_cards() -> None:
    """`AIB_DISABLE_LLM=1` 现场演示：理财与卡片的口语查询必须也有规则接住。"""
    expected = {
        "查一下我的理财": "wealth.list",
        "我的理财": "wealth.list",
        "查持仓": "wealth.list",
        "查一下我的银行卡": "card.list",
        "查一下我的卡片": "card.list",
        "卡片状态": "card.list",
    }
    for utterance, action in expected.items():
        assert _actions(utterance) == [action], utterance


def test_wealth_and_card_query_rules_keep_frozen_intents() -> None:
    """补查询规则不得抢走：余额查询、失卡止损、理财申购 / 赎回。"""
    assert _actions("查一下我的银行卡余额") == ["account.balance"]
    assert _actions("我的银行卡丢了") == ["card.report_lost"]
    assert _actions("买理财") == ["wealth.subscribe"]
    assert _actions("赎回理财") == ["wealth.redeem"]


def test_wealth_query_rule_keeps_hallucination_trap_blocked() -> None:
    """对抗样例 `adv-hallucination-5`：「不存在的理财收益」不得被理财查询规则接走。"""
    assert _actions("把我不存在的理财收益提出来") == []


@pytest.mark.parametrize(
    "utterance",
    [
        "取消后商户还扣吗",
        "取消后商户还会扣吗",
        "取消后还会扣款吗",
        "取消后还会继续扣费吗",
        "取消以后还会扣吗",
    ],
)
def test_cancel_followup_colloquial_phrasings_map_to_subscription_list(
    utterance: str,
) -> None:
    """取消后的口语追问（「还会扣 / 还会继续扣费」）必须归查询，不能掉进未知意图或写动作。"""
    actions = _actions(utterance)
    assert actions and actions[0] == "subscription.list", utterance
    assert "subscription.cancel" not in actions, utterance


def test_card_region_order_is_frozen() -> None:
    patterns = [pattern.pattern for pattern, _, _ in _TEMPLATE_RULES]
    unlock_index = next(index for index, pattern in enumerate(patterns) if "解锁" in pattern)
    direct_lock_index = next(
        index for index, pattern in enumerate(patterns) if "锁.{0,3}卡" in pattern
    )
    existing_lock_index = patterns.index("锁卡|限制.*卡|卡.*限制")
    assert unlock_index < direct_lock_index < existing_lock_index
