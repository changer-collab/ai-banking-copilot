"""失卡四步（锁定 → 挂失 → 补虚拟卡 → 改密码）中途失败：不得把会话 / 卡片留在死锁态。

验收口径（`docs/2026-10-27/分工文档-2026-10-27.md` §5.4）：
- 某一步失败（本例为补虚拟卡）→ 已完成的步骤保留，状态为 `PARTIAL`，如实回报；
- 卡不会停在「锁定」这个半截状态（挂失已执行则为 `lost`，未执行则仍可重新发起）；
- 会话不被卡死：可以在同一会话重新提交并完成「补虚拟卡」，拿到新卡。
"""

from __future__ import annotations

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session, SessionState


def test_failed_virtual_card_step_keeps_progress_without_deadlock(
    deps: AppDeps,
    gateway: FactGateway,
    monkeypatch,
) -> None:
    session = Session(deps)
    view = session.submit("我卡丢了", selected={"card": "card_debit"})
    assert view.plan is not None
    assert [node.action for node in view.plan.nodes][:4] == [
        "card.lock",
        "card.report_lost",
        "card.apply_virtual",
        "password.change",
    ]
    assert view.card is not None
    assert view.card.level is Level.RED

    # 补虚拟卡第一次调用失败：业务写服务抛错，后续步骤不得继续。
    original = CoreBank.apply_virtual_card

    def flaky(self: CoreBank, *args: object, **kwargs: object) -> object:
        raise RuntimeError("虚拟卡通道暂不可用")

    monkeypatch.setattr(CoreBank, "apply_virtual_card", flaky)

    prompt = session.request_verification()
    executed = session.confirm(
        view.card.card_id,
        frozen_hash=view.card.frozen_hash,
        verification_code=prompt.code,
    )

    # 已执行的两步保留；失败如实回报为 PARTIAL（不是假装完成，也不是永久 EXECUTING）。
    assert [item.action for item in executed.exec_results] == ["card.lock", "card.report_lost"]
    assert executed.state is SessionState.PARTIAL
    assert "虚拟卡通道暂不可用" in executed.message
    # 挂失是不可逆步骤，终态必须是 lost，而不是停在「已锁定」。
    assert gateway.card("card_debit").payload.status == "lost"
    # 没有冻结额悬空（失卡链路不产生 hold；这里断言通用兜底口径）。
    assert not list(gateway.active_holds_of(deps.user_id))

    # 会话没有被卡死：同一会话可以重新提交缺失的那一步。
    monkeypatch.setattr(CoreBank, "apply_virtual_card", original)
    retry = session.submit(
        "申请虚拟卡",
        selected={"daily_limit": Value(Money(50000), Trust.TRUSTED)},
    )
    assert retry.state is SessionState.AWAITING_CONFIRM
    assert retry.card is not None
    assert retry.card.level is Level.YELLOW
    done = session.confirm(retry.card.card_id, frozen_hash=retry.card.frozen_hash)
    assert done.state is SessionState.COMPLETED

    cards = [record.payload for record in gateway.cards_of(deps.user_id)]
    virtual = [card for card in cards if card.card_type == "virtual"]
    assert virtual, "补虚拟卡应当成功落库"
    assert virtual[-1].daily_limit.cents == 50000
