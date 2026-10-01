import pytest

from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session, SessionState, SessionStateError

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
