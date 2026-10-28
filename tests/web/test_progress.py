"""问答阶段进度：只记录真实阶段，终态即停，超时自动失效，按用户隔离。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_banking_copilot.web.progress import (
    PROGRESS_TTL_SECONDS,
    STAGE_LABELS,
    STAGES,
    ProgressRegistry,
)

_T0 = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


def test_start_then_advance_exposes_active_step() -> None:
    registry = ProgressRegistry(clock=lambda: _T0)
    registry.start("u_demo", "s1", "r1")

    state = registry.get("u_demo", "s1", "r1")
    assert state is not None
    assert state.stage == STAGES[0]
    assert state.finished is False

    registry.advance("u_demo", "s1", "r1", "plan")
    payload = registry.get("u_demo", "s1", "r1")
    assert payload is not None
    stages = payload.to_dict()["stages"]
    assert [item["key"] for item in stages] == list(STAGES)
    assert [item["label"] for item in stages] == [STAGE_LABELS[key] for key in STAGES]
    assert [item["done"] for item in stages] == [True, True, False, False, False]
    assert [item["active"] for item in stages] == [False, False, True, False, False]


def test_finish_marks_every_step_done() -> None:
    registry = ProgressRegistry(clock=lambda: _T0)
    registry.start("u_demo", "s1", "r1")
    registry.finish("u_demo", "s1", "r1")

    state = registry.get("u_demo", "s1", "r1")
    assert state is not None
    assert state.finished is True
    payload = state.to_dict()
    assert payload["finished"] is True
    assert all(item["done"] for item in payload["stages"])
    assert not any(item["active"] for item in payload["stages"])


def test_unknown_stage_is_rejected() -> None:
    registry = ProgressRegistry(clock=lambda: _T0)
    with pytest.raises(ValueError):
        registry.advance("u_demo", "s1", "r1", "unknown")


def test_entries_are_isolated_by_user_and_request() -> None:
    registry = ProgressRegistry(clock=lambda: _T0)
    registry.start("u_demo", "s1", "r1")

    assert registry.get("u_lin", "s1", "r1") is None
    assert registry.get("u_demo", "s2", "r1") is None
    assert registry.get("u_demo", "s1", "r2") is None


def test_entry_expires_after_ttl() -> None:
    moment = [_T0]
    registry = ProgressRegistry(clock=lambda: moment[0])
    registry.start("u_demo", "s1", "r1")

    moment[0] = _T0 + timedelta(seconds=PROGRESS_TTL_SECONDS)
    assert registry.get("u_demo", "s1", "r1") is None


def test_clear_and_rewrite_are_independent() -> None:
    registry = ProgressRegistry(clock=lambda: _T0)
    registry.start("u_demo", "s1", "r1")
    registry.clear("u_demo", "s1", "r1")
    assert registry.get("u_demo", "s1", "r1") is None

    registry.start("u_demo", "s1", "r1")
    registry.start("u_demo", "s1", "r1")
    assert registry.get("u_demo", "s1", "r1") is not None
