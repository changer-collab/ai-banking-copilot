from __future__ import annotations

import json
import runpy
from datetime import UTC, datetime

import pytest

from ai_banking_copilot.planning.reviewer import build_reviewer_chat_payload
from ai_banking_copilot.planning.transport_probe import (
    ProbeAttempt,
    ProbeClass,
    ProbeResult,
    probe_chat_completion,
)

_DIAGNOSTIC = runpy.run_path("scripts/diagnose_llm_transport.py")
run_diagnostics = _DIAGNOSTIC["run_diagnostics"]
main = _DIAGNOSTIC["main"]


def _result(channel: str, classification: ProbeClass, text: str | None = None) -> ProbeResult:
    envelope = (
        json.dumps({"choices": [{"message": {"content": text}}]}) if text is not None else None
    )
    return ProbeResult(
        channel=channel,
        classification=classification,
        attempts=(ProbeAttempt(1, classification, 1.0),),
        model_text=text,
        raw_response=envelope,
    )


def _planner_text(action: str = "bills.monthly") -> str:
    return json.dumps(
        {
            "intents": [
                {
                    "action": action,
                    "confidence": 1.0,
                    "evidence": "查账单",
                    "slots": {"span": "上月账单"},
                }
            ]
        },
        ensure_ascii=False,
    )


def _review_payload(payload: dict[str, object]) -> dict[str, object]:
    messages = payload["messages"]
    assert isinstance(messages, list)
    assert [message["role"] for message in messages] == ["system", "user"]
    content = json.loads(messages[1]["content"])
    assert isinstance(content, dict)
    return content


def _reviewer_text(payload: dict[str, object]) -> str:
    review = _review_payload(payload)
    return json.dumps(
        {
            "execution_id": review["execution_id"],
            "cfhash": review["cfhash"],
            "prompt_version": review["prompt_version"],
            "status": "allow",
            "findings": [],
            "clarifications": [],
        }
    )


def _set_key(monkeypatch: pytest.MonkeyPatch, value: str = "sensitive-key") -> None:
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)
    monkeypatch.delenv("AI_TIMEOUT_S", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", value)


def test_missing_key_stops_before_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AI_API_KEY", "DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def unexpected_probe(**_kwargs: object) -> ProbeResult:
        raise AssertionError("probe called without key")

    report = run_diagnostics(probe=unexpected_probe)
    assert report["planner"]["classification"] == "config_missing"
    assert report["reviewer"]["classification"] == "config_missing"
    assert report["protocol_checks"] == {"planner": None, "reviewer": None}
    assert report["reviewer_binding_check"] is None
    assert report["ready_for_smoke"] is False


@pytest.mark.parametrize(
    "endpoint", [None, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"]
)
def test_deepseek_key_never_reaches_dashscope(
    monkeypatch: pytest.MonkeyPatch, endpoint: str | None
) -> None:
    for name in ("AI_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    if endpoint is None:
        monkeypatch.delenv("AI_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("AI_BASE_URL", endpoint)

    def unexpected_probe(**_kwargs: object) -> ProbeResult:
        raise AssertionError("mismatched credential reached network")

    report = run_diagnostics(probe=unexpected_probe)
    assert report["planner"]["classification"] == "config_missing"
    assert report["reviewer"]["classification"] == "config_missing"
    assert report["config_reason"] == "DEEPSEEK_API_KEY 与 DashScope 端点不匹配"
    assert "deepseek-secret" not in json.dumps(report)


@pytest.mark.parametrize(
    "endpoint",
    ["https://api.deepseek.com/chat/completions", "https://proxy.example.test/chat/completions"],
)
def test_explicit_deepseek_endpoint_runs_both_channels(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    for name in ("AI_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("AI_BASE_URL", endpoint)
    monkeypatch.setenv("AI_MODEL", "deepseek-flash")
    calls: list[str] = []

    def fake_probe(**kwargs: object) -> ProbeResult:
        channel = str(kwargs["channel"])
        calls.append(channel)
        assert kwargs["endpoint"] == endpoint
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        assert payload["model"] == "deepseek-flash"
        if channel == "planner":
            return _result(channel, ProbeClass.OK, _planner_text())
        return _result(channel, ProbeClass.OK, _reviewer_text(payload))

    report = run_diagnostics(probe=fake_probe)
    assert calls == ["planner", "reviewer"]
    assert report["ready_for_smoke"] is True
    assert "deepseek-secret" not in json.dumps(report)


def test_dashscope_key_never_reaches_deepseek(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    monkeypatch.setenv("AI_BASE_URL", "https://api.deepseek.com/chat/completions")

    def unexpected_probe(**_kwargs: object) -> ProbeResult:
        raise AssertionError("mismatched credential reached network")

    report = run_diagnostics(probe=unexpected_probe)
    assert report["planner"]["classification"] == "config_missing"
    assert report["reviewer"]["classification"] == "config_missing"
    assert report["config_reason"] == "DASHSCOPE_API_KEY 与 DeepSeek 端点不匹配"


@pytest.mark.parametrize("endpoint", ["not-a-url", "ftp://example.com"])
def test_invalid_endpoint_stops_before_probe(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    _set_key(monkeypatch)
    monkeypatch.setenv("AI_BASE_URL", endpoint)

    def unexpected_probe(**_kwargs: object) -> ProbeResult:
        raise AssertionError("invalid endpoint reached network")

    report = run_diagnostics(probe=unexpected_probe)
    assert report["planner"]["classification"] == "config_missing"
    assert report["reviewer"]["classification"] == "config_missing"
    assert report["config_reason"] == "AI_BASE_URL 必须是带主机名的 HTTP(S) 地址"


def test_report_has_utc_time_and_separate_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)

    def fake_probe(**kwargs: object) -> ProbeResult:
        channel = str(kwargs["channel"])
        if channel == "planner":
            return _result(channel, ProbeClass.OK, _planner_text())
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        return _result(channel, ProbeClass.OK, _reviewer_text(payload))

    report = run_diagnostics(probe=fake_probe)
    assert datetime.fromisoformat(report["run_at_utc"]).tzinfo == UTC
    assert report["protocol_checks"] == {"planner": True, "reviewer": True}
    assert report["reviewer_binding_check"] is True


def test_default_timeout_keeps_planner_20_and_reviewer_180(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_key(monkeypatch)
    calls: list[tuple[object, object, object]] = []

    def fake_probe(**kwargs: object) -> ProbeResult:
        channel = str(kwargs["channel"])
        calls.append((channel, kwargs["timeout_s"], kwargs["max_attempts"]))
        if channel == "planner":
            return _result(channel, ProbeClass.OK, _planner_text())
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        return _result(channel, ProbeClass.OK, _reviewer_text(payload))

    report = run_diagnostics(probe=fake_probe)
    assert calls == [("planner", 20.0, 1), ("reviewer", 180.0, 1)]
    assert report["timeout_s"] == 20.0
    assert report["reviewer_timeout_s"] == 180.0
    assert report["ready_for_smoke"] is True


def test_explicit_timeout_overrides_both_diagnostic_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_key(monkeypatch)
    monkeypatch.setenv("AI_TIMEOUT_S", "7.0")
    calls: list[tuple[object, object, object]] = []

    def fake_probe(**kwargs: object) -> ProbeResult:
        channel = str(kwargs["channel"])
        calls.append((channel, kwargs["timeout_s"], kwargs["max_attempts"]))
        if channel == "planner":
            return _result(channel, ProbeClass.OK, _planner_text())
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        return _result(channel, ProbeClass.OK, _reviewer_text(payload))

    report = run_diagnostics(probe=fake_probe)
    assert calls == [("planner", 7.0, 1), ("reviewer", 7.0, 1)]
    assert report["timeout_s"] == 7.0
    assert report["reviewer_timeout_s"] == 7.0
    assert report["ready_for_smoke"] is True


def test_both_channels_use_production_payload_and_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    calls: list[dict[str, object]] = []

    def fake_probe(**kwargs: object) -> ProbeResult:
        calls.append(kwargs)
        channel = str(kwargs["channel"])
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        assert payload["model"] == "qwen-plus"
        if channel == "planner":
            messages = payload["messages"]
            assert isinstance(messages, list)
            assert messages[1]["role"] == "user"
            assert "【用户原话】\n查一下上月账单\n" in messages[1]["content"]
            return _result(channel, ProbeClass.OK, _planner_text())
        assert _review_payload(payload)["candidate_actions"] == ["bills.monthly"]
        assert payload == build_reviewer_chat_payload(
            payload["messages"][1]["content"], model="qwen-plus"
        )
        return _result(channel, ProbeClass.OK, _reviewer_text(payload))

    report = run_diagnostics(probe=fake_probe)
    assert [call["channel"] for call in calls] == ["planner", "reviewer"]
    assert report["planner"]["classification"] == "ok"
    assert report["reviewer"]["classification"] == "ok"
    assert report["ready_for_smoke"] is True
    output = json.dumps(report, ensure_ascii=False)
    assert "sensitive-key" not in output
    assert "查一下上月账单" not in output
    assert "model_text" not in output


@pytest.mark.parametrize(
    "model_text", ["not-json", '{"intents":[]}', _planner_text("not.in.catalog")]
)
def test_planner_output_must_contain_a_valid_intent(
    monkeypatch: pytest.MonkeyPatch, model_text: str
) -> None:
    _set_key(monkeypatch)

    def fake_probe(**kwargs: object) -> ProbeResult:
        channel = str(kwargs["channel"])
        if channel == "planner":
            return _result(channel, ProbeClass.OK, model_text)
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        return _result(channel, ProbeClass.OK, _reviewer_text(payload))

    report = run_diagnostics(probe=fake_probe)
    assert report["planner"]["classification"] == "model_output_error"
    assert report["reviewer"]["classification"] == "ok"
    assert report["ready_for_smoke"] is False


@pytest.mark.parametrize("reviewer_text", ["not-json", "wrong-binding"])
def test_reviewer_binding_or_format_failure_is_protocol_error(
    monkeypatch: pytest.MonkeyPatch, reviewer_text: str
) -> None:
    _set_key(monkeypatch)

    def fake_probe(**kwargs: object) -> ProbeResult:
        channel = str(kwargs["channel"])
        if channel == "planner":
            return _result(channel, ProbeClass.OK, _planner_text())
        if reviewer_text == "not-json":
            return _result(channel, ProbeClass.OK, "not-json secret-model-body")
        return _result(
            channel,
            ProbeClass.OK,
            json.dumps(
                {
                    "execution_id": "wrong-execution",
                    "cfhash": "0" * 64,
                    "prompt_version": "reviewer-v1",
                    "status": "allow",
                }
            ),
        )

    report = run_diagnostics(probe=fake_probe)
    assert report["reviewer"]["classification"] == "protocol_error"
    assert report["ready_for_smoke"] is False
    assert "secret-model-body" not in json.dumps(report)
    if reviewer_text == "not-json":
        assert report["protocol_checks"]["reviewer"] is False
        assert report["reviewer_binding_check"] is None
    else:
        assert report["protocol_checks"]["reviewer"] is True
        assert report["reviewer_binding_check"] is False


def test_transport_error_stays_in_its_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)

    def fake_probe(**kwargs: object) -> ProbeResult:
        channel = str(kwargs["channel"])
        if channel == "planner":
            return _result(channel, ProbeClass.OK, _planner_text())
        return _result(channel, ProbeClass.TRANSPORT_ERROR)

    report = run_diagnostics(probe=fake_probe)
    assert report["planner"]["classification"] == "ok"
    assert report["reviewer"]["classification"] == "transport_error"
    assert report["protocol_checks"]["reviewer"] is None
    assert report["reviewer_binding_check"] is None
    assert report["ready_for_smoke"] is False


def test_reviewer_http_rejection_keeps_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)

    def fake_probe(**kwargs: object) -> ProbeResult:
        channel = str(kwargs["channel"])
        if channel == "planner":
            return _result(channel, ProbeClass.OK, _planner_text())
        return _result(channel, ProbeClass.HTTP_ERROR)

    report = run_diagnostics(probe=fake_probe)
    assert report["reviewer"]["classification"] == "http_error"
    assert report["protocol_checks"]["reviewer"] is None
    assert report["reviewer_binding_check"] is None


@pytest.mark.parametrize("timeout", ["not-a-number", "0", "-2"])
def test_invalid_timeout_stops_before_probe(monkeypatch: pytest.MonkeyPatch, timeout: str) -> None:
    _set_key(monkeypatch)
    monkeypatch.setenv("AI_TIMEOUT_S", timeout)

    def unexpected_probe(**_kwargs: object) -> ProbeResult:
        raise AssertionError("invalid timeout reached network")

    report = run_diagnostics(probe=unexpected_probe)
    assert report["planner"]["classification"] == "config_missing"
    assert report["reviewer"]["classification"] == "config_missing"
    assert report["config_reason"] == "AI_TIMEOUT_S 必须为正数"


def test_json_out_must_stay_under_runtime() -> None:
    assert main(["--json-out", "outside-diagnostic-report.json"]) != 0


def test_planner_bad_text_then_valid_uses_two_total_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_key(monkeypatch)
    calls: dict[str, int] = {"planner": 0, "reviewer": 0}

    def fake_probe(**kwargs: object) -> ProbeResult:
        assert kwargs["max_attempts"] == 1
        channel = str(kwargs["channel"])
        calls[channel] += 1
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        if channel == "planner":
            return _result(
                channel, ProbeClass.OK, "not-json" if calls[channel] == 1 else _planner_text()
            )
        return _result(channel, ProbeClass.OK, _reviewer_text(payload))

    report = run_diagnostics(probe=fake_probe)
    assert calls == {"planner": 2, "reviewer": 1}
    assert report["planner"]["classification"] == "ok"
    assert [item["classification"] for item in report["planner"]["attempts"]] == [
        "model_output_error",
        "ok",
    ]
    assert report["ready_for_smoke"] is True


def test_reviewer_bad_binding_then_valid_uses_two_total_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_key(monkeypatch)
    calls = {"planner": 0, "reviewer": 0}

    def fake_probe(**kwargs: object) -> ProbeResult:
        assert kwargs["max_attempts"] == 1
        channel = str(kwargs["channel"])
        calls[channel] += 1
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        if channel == "planner":
            return _result(channel, ProbeClass.OK, _planner_text())
        content = _reviewer_text(payload)
        if calls[channel] == 1:
            execution_id = str(_review_payload(payload)["execution_id"])
            content = content.replace(execution_id, "wrong-execution")
        return _result(channel, ProbeClass.OK, content)

    report = run_diagnostics(probe=fake_probe)
    assert calls == {"planner": 1, "reviewer": 2}
    assert [item["classification"] for item in report["reviewer"]["attempts"]] == [
        "protocol_error",
        "ok",
    ]
    assert report["ready_for_smoke"] is True


def test_provider_usage_details_accepted_by_production_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_key(monkeypatch)
    calls = 0

    def fake_probe(**kwargs: object) -> ProbeResult:
        nonlocal calls
        channel = str(kwargs["channel"])
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        if channel == "planner":
            return _result(channel, ProbeClass.OK, _planner_text())
        calls += 1
        content = _reviewer_text(payload)
        return ProbeResult(
            channel=channel,
            classification=ProbeClass.OK,
            attempts=(ProbeAttempt(1, ProbeClass.OK, 1.0, 200),),
            model_text=content,
            raw_response=json.dumps(
                {
                    "choices": [{"message": {"content": content}}],
                    "usage": {
                        "total_tokens": 3,
                        "prompt_tokens_details": {"cached_tokens": 1},
                        "completion_tokens_details": {"reasoning_tokens": 1},
                    },
                }
            ),
        )

    report = run_diagnostics(probe=fake_probe)
    assert calls == 1
    assert report["reviewer"]["classification"] == "ok"
    assert report["protocol_checks"]["reviewer"] is True
    assert report["reviewer_binding_check"] is True
    assert report["ready_for_smoke"] is True
    assert "prompt_tokens_details" not in json.dumps(report)


def test_probe_protocol_error_sets_checks_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch)
    calls = {"planner": 0, "reviewer": 0}

    def fake_probe(**kwargs: object) -> ProbeResult:
        channel = str(kwargs["channel"])
        calls[channel] += 1
        return _result(channel, ProbeClass.PROTOCOL_ERROR)

    report = run_diagnostics(probe=fake_probe)
    assert calls == {"planner": 2, "reviewer": 2}
    assert report["protocol_checks"] == {"planner": False, "reviewer": False}
    assert [item["number"] for item in report["planner"]["attempts"]] == [1, 2]


def test_two_failed_http_posts_per_channel_is_the_total_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_key(monkeypatch)
    posts = {"planner": 0, "reviewer": 0}

    class Response:
        status_code = 503
        text = "provider unavailable"

    class Client:
        def __init__(self, channel: str) -> None:
            self.channel = channel

        def post(self, *_args: object, **_kwargs: object) -> Response:
            posts[self.channel] += 1
            return Response()

    def local_probe(**kwargs: object) -> ProbeResult:
        channel = str(kwargs["channel"])
        return probe_chat_completion(
            channel=channel,
            endpoint=str(kwargs["endpoint"]),
            api_key=str(kwargs["api_key"]),
            payload=kwargs["payload"],  # type: ignore[arg-type]
            timeout_s=float(kwargs["timeout_s"]),
            max_attempts=int(kwargs["max_attempts"]),
            client_factory=lambda *, timeout: Client(channel),
        )

    report = run_diagnostics(probe=local_probe)
    assert posts == {"planner": 2, "reviewer": 2}
    for channel in posts:
        assert report[channel]["classification"] == "http_error"
        assert [item["http_status"] for item in report[channel]["attempts"]] == [503, 503]
