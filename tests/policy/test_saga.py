from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from ai_banking_copilot.policy.saga import (
    CompensationError,
    ReversalState,
    ReversalWindow,
    Saga,
)

_NOW = datetime(2026, 10, 1, 9, 0, 0)


def _recorder(calls: list[str], tag: str) -> Callable[[], str]:
    def record() -> str:
        calls.append(tag)
        return tag

    return record


def test_run_executes_steps_in_order() -> None:
    calls: list[str] = []
    saga = Saga()
    saga.add("a", _recorder(calls, "a"), _recorder(calls, "-a"))
    saga.add("b", _recorder(calls, "b"), _recorder(calls, "-b"))
    assert saga.run() == ("a", "b")
    assert calls == ["a", "b"]
    assert saga.completed() == ("a", "b")


def test_run_stops_at_failure_and_compensate_rolls_back_in_reverse() -> None:
    calls: list[str] = []
    saga = Saga()
    saga.add("a", _recorder(calls, "a"), _recorder(calls, "-a"))

    def boom() -> str:
        raise RuntimeError("核心银行拒绝")

    saga.add("b", boom, _recorder(calls, "-b"))
    with pytest.raises(RuntimeError, match="拒绝"):
        saga.run()
    assert saga.completed() == ("a",)
    assert saga.compensate() == ("a",)
    assert calls == ["a", "-a"]
    assert saga.completed() == ()


def test_compensation_error_keeps_step_pending() -> None:
    calls: list[str] = []
    saga = Saga()
    saga.add("a", _recorder(calls, "a"), _recorder(calls, "-a"))

    def bad_rollback() -> str:
        raise CompensationError("退回失败")

    saga.add("b", _recorder(calls, "b"), bad_rollback)
    assert saga.run() == ("a", "b")
    with pytest.raises(CompensationError, match="退回失败"):
        saga.compensate()
    assert saga.completed() == ("a", "b")


def test_compensate_wraps_unexpected_error() -> None:
    saga = Saga()

    def broken() -> str:
        raise KeyError("boom")

    saga.add("a", _recorder([], "a"), broken)
    saga.run()
    with pytest.raises(CompensationError, match="补偿 a 失败"):
        saga.compensate()


def test_saga_rejects_duplicate_and_empty_names() -> None:
    saga = Saga()
    saga.add("a", _recorder([], "a"), _recorder([], "-a"))
    with pytest.raises(ValueError, match="重复"):
        saga.add("a", _recorder([], "a2"), _recorder([], "-a2"))
    with pytest.raises(ValueError, match="不得为空"):
        saga.add("", _recorder([], "x"), _recorder([], "-x"))


def test_window_open_sets_deadline() -> None:
    window = ReversalWindow.open("sub_video", now=_NOW, ttl=timedelta(hours=24))
    assert window.deadline == _NOW + timedelta(hours=24)
    assert window.expired(_NOW) is False
    assert window.expired(_NOW + timedelta(hours=24)) is True


def test_window_rejects_non_positive_ttl() -> None:
    with pytest.raises(ValueError, match="TTL"):
        ReversalWindow.open("sub_video", now=_NOW, ttl=timedelta(0))


def test_reverse_before_settlement_is_reversed() -> None:
    window = ReversalWindow.open("sub_video", now=_NOW)
    outcome = window.reverse(now=_NOW, settled=False)
    assert outcome.state is ReversalState.REVERSED
    assert "已取消" not in outcome.message


def test_reverse_after_settlement_compensates_without_claiming_cancelled() -> None:
    calls: list[str] = []
    saga = Saga()
    saga.add("charge", _recorder(calls, "charge"), _recorder(calls, "refund"))
    saga.run()
    window = ReversalWindow.open("sub_video", now=_NOW)
    outcome = window.reverse(now=_NOW, settled=True, compensate=saga.compensate)
    assert outcome.state is ReversalState.COMPENSATING
    assert "已扣" in outcome.message
    assert "退回" in outcome.message
    assert "已取消" not in outcome.message
    assert outcome.compensated_steps == ("charge",)


def test_reverse_after_settlement_requires_compensation() -> None:
    window = ReversalWindow.open("sub_video", now=_NOW)
    with pytest.raises(CompensationError, match="补偿"):
        window.reverse(now=_NOW, settled=True)


def test_reverse_after_expiry_is_honest() -> None:
    window = ReversalWindow.open("sub_video", now=_NOW, ttl=timedelta(minutes=30))
    outcome = window.reverse(now=_NOW + timedelta(hours=1), settled=True)
    assert outcome.state is ReversalState.EXPIRED
    assert "已取消" not in outcome.message


def test_reverse_reports_compensation_failure() -> None:
    window = ReversalWindow.open("sub_video", now=_NOW)

    def broken() -> tuple[str, ...]:
        raise CompensationError("上游不可用")

    outcome = window.reverse(now=_NOW, settled=True, compensate=broken)
    assert outcome.state is ReversalState.FAILED
    assert "未宣称已取消" in outcome.message
