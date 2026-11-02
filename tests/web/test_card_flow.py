"""失卡对话：先澄清「临时冻结 / 正式挂失」，再只执行对应的单节点。

验收口径（`docs/2026-10-27/分工文档-2026-10-27.md` §5.4 的失卡链路修订）：
- 「我的借记卡丢了」不替用户在两个互斥口径之间猜：临时冻结可解，正式挂失不可逆；
- 补充「正式挂失」后只生成 1 个节点，确认执行后卡片如实落到 `lost`。
"""

from __future__ import annotations

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session, SessionState


def test_l5_formal_flow_executes_one_node(deps: AppDeps, gateway: FactGateway) -> None:
    session = Session(deps)
    view = session.submit("我的借记卡丢了", selected={"card": "card_debit"})
    assert view.state is SessionState.NEEDS_CLARIFICATION
    assert [ask.slot for ask in view.clarifications] == ["loss_type"]

    view = session.clarify({"loss_type": "formal"})
    assert view.state is SessionState.AWAITING_CONFIRM
    assert view.plan is not None
    assert [node.action for node in view.plan.nodes] == ["card.report_lost"]
    assert view.card is not None

    prompt = session.request_verification()
    done = session.confirm(
        view.card.card_id,
        frozen_hash=view.card.frozen_hash,
        verification_code=prompt.code,
    )
    assert done.state is SessionState.COMPLETED
    assert gateway.card("card_debit").payload.status == "lost"


def test_card_form_submit_is_not_hijacked_by_pending_payee(deps: AppDeps) -> None:
    session = Session(deps)
    pending = session.submit("转账 300 元")
    assert pending.state is SessionState.NEEDS_CLARIFICATION
    assert "收款人" in pending.message
    view = session.submit(
        "申请补卡",
        selected={
            "card": "card_debit",
            "address": "上海市浦东新区世纪大道 100 号",
            "mail_method": "standard",
            "replace": "yes",
        },
        new_intent=True,
    )
    assert view.state is SessionState.AWAITING_CONFIRM
    assert [node.action for node in view.plan.nodes] == ["card.replace"]
    assert "收款人" not in view.message
