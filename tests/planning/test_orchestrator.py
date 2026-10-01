import pytest

from ai_banking_copilot.planning.orchestrator import (
    StepDriver,
    StepState,
    StepTransition,
)


def test_every_transition_passes_through_the_audit_hook() -> None:
    seen: list[StepTransition] = []
    driver = StepDriver(on_transition=seen.append)
    driver.run(["s1", "s2"], worker=lambda step_id: None)
    assert [item.step_id for item in seen] == ["s1", "s1", "s2", "s2"]
    assert [item.after for item in seen] == [
        StepState.RUNNING,
        StepState.DONE,
        StepState.RUNNING,
        StepState.DONE,
    ]
    assert driver.state_of("s1") is StepState.DONE


def test_authorize_hook_is_called_for_every_transition() -> None:
    calls: list[StepTransition] = []

    def _allow(transition: StepTransition) -> bool:
        calls.append(transition)
        return True

    driver = StepDriver(on_transition=lambda transition: None, authorize=_allow)
    driver.run(["s1"], worker=lambda step_id: None)
    assert len(calls) == 2


def test_denied_transition_is_audited_but_never_applied() -> None:
    seen: list[StepTransition] = []

    def _deny_start(transition: StepTransition) -> bool:
        return transition.after is not StepState.RUNNING

    driver = StepDriver(on_transition=seen.append, authorize=_deny_start)
    with pytest.raises(PermissionError):
        driver.run(["s1"], worker=lambda step_id: None)
    assert [item.after for item in seen] == [StepState.RUNNING]
    assert driver.state_of("s1") is StepState.PENDING


def test_failed_step_is_audited_as_failed_and_propagates() -> None:
    seen: list[StepTransition] = []

    def _boom(step_id: str) -> None:
        raise RuntimeError("boom")

    driver = StepDriver(on_transition=seen.append)
    with pytest.raises(RuntimeError, match="boom"):
        driver.run(["s1"], worker=_boom)
    assert [item.after for item in seen] == [StepState.RUNNING, StepState.FAILED]
    assert driver.state_of("s1") is StepState.FAILED
