import pytest

from ai_banking_copilot.policy.audit import KIND_REVERSAL
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import (
    ExecutionView,
    Session,
    SessionState,
    SessionStateError,
)

CROSS_UTTERANCE = "冻结 500 元，然后转给李娜 300 元"
CROSS_SELECTED = {"account": "acc_savings", "grant": "explicit"}


def test_cross_submit_builds_a_two_step_dag(deps: AppDeps) -> None:
    view = Session(deps).submit(CROSS_UTTERANCE, selected=CROSS_SELECTED)
    assert view.state is SessionState.AWAITING_CONFIRM
    assert view.plan is not None
    assert [node.action for node in view.plan.nodes] == ["funds.freeze", "transfer.create"]
    assert view.plan.nodes[1].depends_on == ("n1",)


def test_interrupt_keeps_completed_steps_and_resume_finishes(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit(CROSS_UTTERANCE, selected=CROSS_SELECTED)
    assert view.card is not None
    first = session.confirm(view.card.card_id, frozen_hash=view.card.frozen_hash, steps=1)
    assert first.state is SessionState.EXECUTING
    assert len(first.exec_results) == 1
    paused = session.interrupt()
    assert paused.state is SessionState.INTERRUPTED
    assert "已中断" in paused.message
    done = session.resume()
    assert done.state is SessionState.COMPLETED
    assert len(done.exec_results) == 2


def test_interrupt_requires_an_executable_session(deps: AppDeps) -> None:
    session = Session(deps)
    with pytest.raises(SessionStateError):
        session.interrupt()


def test_resume_requires_interrupted_state(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit("转给李娜 500 元")
    assert view.card is not None
    with pytest.raises(SessionStateError):
        session.resume()


def test_double_resume_is_rejected(deps: AppDeps) -> None:
    session = Session(deps)
    view = session.submit(CROSS_UTTERANCE, selected=CROSS_SELECTED)
    assert view.card is not None
    session.confirm(view.card.card_id, frozen_hash=view.card.frozen_hash, steps=1)
    session.interrupt()
    assert session.resume().state is SessionState.COMPLETED
    with pytest.raises(SessionStateError):
        session.resume()


def _freeze_hold_id(first_execution: ExecutionView) -> str:
    return next(
        record for record in first_execution.record_ids if record.startswith("holds:")
    )


def _frozen_session(deps: AppDeps) -> tuple[Session, str]:
    """跑完冻结、在第二步前中断，返回（会话，冻结记录）。"""
    session = Session(deps)
    view = session.submit(CROSS_UTTERANCE, selected=CROSS_SELECTED)
    assert view.card is not None
    first = session.confirm(view.card.card_id, frozen_hash=view.card.frozen_hash, steps=1)
    assert first.state is SessionState.EXECUTING
    return session, _freeze_hold_id(first)


def test_reject_after_interrupt_releases_the_frozen_budget(deps: AppDeps) -> None:
    """用户中断后选择放弃：已冻结的预算必须退回可用余额，不能悬空。"""
    session, record = _frozen_session(deps)
    session.interrupt()
    assert deps.gateway.hold(record.partition(":")[2]).payload.status == "active"

    view = session.reject()

    assert view.state is SessionState.IDLE
    assert "已退回冻结预算 1 笔" in view.message
    assert deps.gateway.hold(record.partition(":")[2]).payload.status == "released"
    releases = [
        event.payload
        for event in deps.audit.events()
        if event.kind == KIND_REVERSAL and event.payload.get("subject") == "holds"
    ]
    assert releases[-1]["outcome"] == "released_on_failure"
    assert releases[-1]["reason"] == "plan_rejected"
    assert releases[-1]["user_id"] == deps.user_id
    assert releases[-1]["session_id"] == "default"


def test_new_submit_after_interrupt_releases_the_previous_frozen_budget(deps: AppDeps) -> None:
    """中断后直接重新提交：上一计划的冻结必须先退回，否则永远悬空。"""
    session, record = _frozen_session(deps)
    session.interrupt()

    session.submit(CROSS_UTTERANCE, selected=CROSS_SELECTED)

    assert deps.gateway.hold(record.partition(":")[2]).payload.status == "released"


def test_unexpected_execution_error_releases_the_frozen_budget(
    deps: AppDeps, monkeypatch: pytest.MonkeyPatch
) -> None:
    """任何未预期异常都走终态收尾：先退回冻结额，再如实报错。"""
    session, record = _frozen_session(deps)
    session.interrupt()

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("模拟核心银行连接中断")

    monkeypatch.setattr(deps.pep, "execute", _boom)
    view = session.resume()

    assert view.state is SessionState.PARTIAL
    assert "执行失败" in view.message
    assert deps.gateway.hold(record.partition(":")[2]).payload.status == "released"
    releases = [
        event.payload
        for event in deps.audit.events()
        if event.kind == KIND_REVERSAL and event.payload.get("subject") == "holds"
    ]
    assert releases[-1]["outcome"] == "released_on_failure"
    assert releases[-1]["reason"] == "unexpected_error"
    assert releases[-1]["execution_id"]
