"""澄清闭环回归（2026-11-01 实测缺陷）：

- 保密要求是**约束**，不能被当成"账单查询"意图，也不能在澄清轮沉默；
- 订花（L1）问过的字段答完必须被消费，不允许在金额上死循环；
- 一句多字段的自由文本要能一次答完，不能只吃第一个字段；
- 周期 / 条件触发的自动转账没有实现，必须是**能力边界**说明，而不是先追问收款人再抛安全拒绝。
"""

from __future__ import annotations

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session, SessionState

FLOWER = "我爱人生日想给她惊喜，但别在她常看的账单推送里暴露这笔支出"
FORM_ANSWERS = {
    "merchant": "花间集（西湖店）",
    "amount": "299 元",
    "delivery_address": "暨南大学深圳校区",
    "delivery_time": "2026-11-04",
}
FREEFORM = (
    "商户名称：花间集（西湖店）· 距离 0.8 km；金额：经典玫瑰 · 299 元；"
    "配送地址：暨南大学深圳校区；送达时间：2026-11-04"
)


def _actions(session: Session) -> list[str]:
    return [str(item["action"]) for item in session._review_intents]


def _order_amounts(view: object) -> list[object]:
    plan = getattr(view, "plan", None)
    assert plan is not None
    return [
        node.params.get("amount_cents")
        for node in plan.nodes
        if node.action == "merchant.order"
    ]


def test_privacy_request_is_acknowledged_and_not_a_bill_intent(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit(FLOWER)

    assert view.state is SessionState.NEEDS_CLARIFICATION
    # 约束子句不能变成一个账单查询意图
    assert _actions(session) == ["merchant.order"]
    # 也不能在澄清轮沉默
    assert view.visibility == "private"
    assert "不会出现这一笔" in view.message


def test_form_answers_are_all_consumed(deps: AppDeps) -> None:
    session = Session(deps)
    session.submit(FLOWER)
    view = session.clarify(FORM_ANSWERS)

    assert view.state is SessionState.AWAITING_CONFIRM
    assert _order_amounts(view) == [29900]
    for ask in view.clarifications:
        assert ask.slot not in FORM_ANSWERS


def test_freeform_multi_slot_answer_is_consumed(deps: AppDeps) -> None:
    session = Session(deps)
    session.submit(FLOWER)
    view = session.submit(FREEFORM)

    assert view.state is SessionState.AWAITING_CONFIRM
    assert _order_amounts(view) == [29900]


def test_freeform_answer_without_unit_still_works(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("我妈下个月过生日，预算 800 元，商户 鲜花礼品店")

    assert view.state is SessionState.NEEDS_CLARIFICATION
    assert all(ask.slot != "merchant" for ask in view.clarifications)


def test_recurring_transfer_is_a_capability_boundary(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("每月 1 号自动给我妈转 3000")

    assert view.state is SessionState.BLOCKED
    assert "不支持" in view.message and "周期" in view.message
    # 能力边界不计入策略拒绝，不能把会话推去熔断
    assert session._policy_rejections == 0
    assert session._last_utterance is not None


def test_conditional_transfer_is_a_capability_boundary(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("发工资第二天转 5000 去理财")

    assert view.state is SessionState.BLOCKED
    assert "不支持" in view.message
    assert session._policy_rejections == 0


def test_owned_object_tampering_is_still_a_policy_rejection(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("转给李娜 500 元", selected={"payee": "p_not_mine"})

    assert view.state is SessionState.BLOCKED
    assert session._policy_rejections == 1


def test_legacy_payee_name_answer_is_not_a_policy_rejection(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("转给李娜 500 元", selected={"account": "储蓄卡"})

    assert view.state is not SessionState.BLOCKED
    assert session._policy_rejections == 0


def test_gateway_payees_untouched(deps: AppDeps) -> None:
    """澄清链路不得写入名册（事实只读）。"""
    gateway: FactGateway = deps.gateway
    before = len(gateway.payees_of(deps.user_id))
    session = Session(deps)
    session.submit(FLOWER)
    session.submit(FREEFORM)
    assert len(gateway.payees_of(deps.user_id)) == before
