import json
from time import perf_counter

import httpx
import pytest


def emit(text=None, error=None, status=200, observer=None):
    from ai_banking_copilot.planning.call_observation import emit_http_observation

    records = []
    emit_http_observation(
        records.append if observer is None else observer,
        role="planner",
        started_at=perf_counter(),
        http_status=status,
        response_text=text,
        error=error,
    )
    return records


def test_only_provider_usage_is_retained_with_named_details():
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 3,
        "total_tokens": 13,
        "prompt_cache_hit_tokens": 4,
        "prompt_cache_miss_tokens": 6,
        "prompt_tokens_details": {"cached_tokens": 4},
        "completion_tokens_details": {"reasoning_tokens": 2},
        "secret": 999,
    }
    record = emit(json.dumps({"usage": usage, "choices": [{"usage": {"total_tokens": 999}}]}))[0]
    assert record.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 3,
        "total_tokens": 13,
        "prompt_cache_hit_tokens": 4,
        "prompt_cache_miss_tokens": 6,
        "prompt_tokens_details.cached_tokens": 4,
        "completion_tokens_details.reasoning_tokens": 2,
    }
    assert record.role == "planner" and record.failure_code is None
    assert record.latency_ms >= 0
    assert set(vars(record)) == {"role", "http_status", "latency_ms", "failure_code", "usage"}


def test_observation_metadata_is_optional_and_keeps_legacy_construction():
    from ai_banking_copilot.planning.call_observation import CallObservation

    old = CallObservation("planner", 200, 1.0, None, None)
    assert old.task_id is None
    detailed = emit('{"usage":{"total_tokens":3}}')[0]
    assert detailed.usage == {"total_tokens": 3}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "bad",
        "[]",
        '{"usage":null}',
        '{"choices":[{"message":{"content":"usage: 9"}}]}',
        '{"usage":{"total_tokens":true,"prompt_tokens":-1,"completion_tokens":"9"}}',
    ],
)
def test_unknown_or_invalid_usage_stays_unknown(raw):
    assert emit(raw)[0].usage is None


def test_partial_usage_is_preserved():
    assert emit('{"usage":{"total_tokens":false,"prompt_tokens":0}}')[0].usage == {
        "prompt_tokens": 0
    }


@pytest.mark.parametrize(
    ("status", "error", "code"),
    [
        (503, None, "http_error"),
        (None, httpx.ReadTimeout("private"), "timeout"),
        (None, TimeoutError("private"), "timeout"),
        (None, httpx.ConnectError("private"), "transport_error"),
    ],
)
def test_errors_have_fixed_codes(status, error, code):
    record = emit(error=error, status=status)[0]
    assert record.failure_code == code
    assert "private" not in repr(record)


def test_observer_exception_is_isolated_but_interrupts_propagate():
    def broken(record):
        raise ValueError("observer")

    emit(observer=broken)

    def interrupt(record):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        emit(observer=interrupt)


def test_disabled_observation_does_not_parse(monkeypatch):
    from ai_banking_copilot.planning import call_observation

    def broken(raw):
        raise AssertionError("must not parse")

    monkeypatch.setattr(call_observation.json, "loads", broken)
    call_observation.emit_http_observation(
        None, role="planner", started_at=0, http_status=200, response_text="secret", error=None
    )
