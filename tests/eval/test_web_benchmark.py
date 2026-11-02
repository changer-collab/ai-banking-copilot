"""Web 回归评测：真实路由、独立业务预期与不掩盖失败的统计。"""

import csv
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ai_banking_copilot.eval.web_benchmark import (
    load_cases,
    run_benchmark,
    summarize,
)


def test_web_cases_cover_six_scenarios_and_authorization_attacks() -> None:
    cases = load_cases()
    # 签约下线后移除了 web-subs-sign 正例：20 条（正例 13 + 攻击 5 + 幂等 1 + 独立提交 1）。
    assert len(cases) == 20
    assert all(case.get("metric_group") for case in cases)
    assert (
        sum(case.get("metric_group", case["kind"]) == "legitimate" for case in cases)
        == 13
    )
    assert sum(case.get("metric_group") == "attack" for case in cases) == 5
    assert sum(case.get("metric_group") == "idempotency" for case in cases) == 1
    assert sum(case.get("metric_group") == "independent_submission" for case in cases) == 1
    assert {case["scenario"] for case in cases if case["kind"] == "legitimate"} >= {
        "transfer",
        "bills",
        "wealth",
        "cards",
        "subscriptions",
        "cross",
    }
    assert {case.get("attack") for case in cases if case["kind"] == "attack"} >= {
        "wrong_code",
        "no_confirmation",
        "tampered_hash",
        "replay",
    }


def test_real_web_execution_has_reproducible_business_results(tmp_path: Path) -> None:
    cases = [
        case
        for case in load_cases()
        if case["id"]
        in {
            "web-transfer-confirm",
            "web-cards-red",
            "web-transfer-tampered-hash",
        }
    ]
    first = run_benchmark(tmp_path / "first", cases=cases)
    second = run_benchmark(tmp_path / "second", cases=cases)
    assert first["business_digest"] == second["business_digest"]
    assert first["passed"] is True
    assert first["summary"]["attack_block_rate"]["denominator"] == 1
    assert first["summary"]["false_rejection_rate"]["numerator"] == 0
    assert first["summary"]["money_trace_rate"]["denominator"] > 0
    assert first["summary"]["idempotency_success_rate"]["denominator"] == 0
    assert (tmp_path / "first" / "results.json").is_file()
    assert (tmp_path / "first" / "summary.csv").is_file()
    assert (tmp_path / "first" / "manifest.json").is_file()


def test_business_snapshots_ignore_session_metadata_but_keep_money_and_receipts(
    tmp_path: Path,
) -> None:
    from contextlib import closing

    from ai_banking_copilot.eval.web_benchmark import _business_snapshot, _snapshot
    from ai_banking_copilot.gateway.store import SCHEMA_SQL, Store

    with closing(Store(tmp_path / "snapshot.db")) as store:
        with store.transaction() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute("INSERT INTO users VALUES ('u', '用户', 'stable')")
            conn.execute("INSERT INTO accounts VALUES ('a', 'u', 'savings', 100, 'CNY')")
            conn.execute("CREATE TABLE session_snapshots (snapshot_json TEXT)")
        before = _snapshot(store), _business_snapshot(store)
        with store.transaction() as conn:
            conn.execute("INSERT INTO session_snapshots VALUES ('random-execution-and-challenge')")
        assert (_snapshot(store), _business_snapshot(store)) == before
        with store.transaction() as conn:
            conn.execute("UPDATE accounts SET balance_cents = 99")
            conn.execute("INSERT INTO write_receipts VALUES ('id', 'transfer.create', '{}')")
        after = _snapshot(store), _business_snapshot(store)
        assert after[0]["accounts"] != before[0]["accounts"]
        assert after[1]["accounts"] != before[1]["accounts"]
        assert after[0]["write_receipts"] != before[0]["write_receipts"]
        assert after[1]["write_receipts"] != before[1]["write_receipts"]


def test_evidence_dev_dataset_has_frozen_coverage() -> None:
    from ai_banking_copilot.eval.web_benchmark import load_dataset

    cases = load_dataset("evidence-dev-v1")
    assert len(cases) >= 90
    assert sum(case.get("metric_group") == "legitimate" for case in cases) >= 48
    assert sum(case.get("metric_group") == "clarification" for case in cases) >= 12
    assert sum(case.get("metric_group") == "attack" for case in cases) >= 30
    categories = {
        case.get("attack_category")
        for case in cases
        if case.get("metric_group") == "attack"
    }
    assert len(categories) >= 6


def test_wrong_business_expectation_fails_even_if_web_completed(tmp_path: Path) -> None:
    case = next(case for case in load_cases() if case["id"] == "web-transfer-confirm")
    case = {
        **case,
        "checks": [
            {
                "name": "错误余额",
                "sql": "SELECT balance_cents FROM accounts WHERE account_id = 'acc_savings'",
                "expected": 1,
            }
        ],
    }
    report = run_benchmark(tmp_path, cases=[case])
    assert report["passed"] is False
    assert report["cases"][0]["state"] == "completed"
    assert report["cases"][0]["failures"]
    assert report["summary"]["task_success_rate"]["numerator"] == 0


def test_changed_displayed_money_fails_value_proof(tmp_path: Path) -> None:
    case = next(case for case in load_cases() if case["id"] == "web-transfer-confirm")
    case = {
        **case,
        "numbers": [
            {
                "pattern": r"金额 (?P<value>[\d.]+) 元",
                "sql": "SELECT 'transactions:'||txn_id, 1 FROM transactions WHERE channel='agent'",
                "unit": "cents",
            }
        ],
    }
    report = run_benchmark(tmp_path, cases=[case])
    assert report["passed"] is False
    assert report["summary"]["money_trace_rate"]["numerator"] == 0


def test_correct_money_without_source_hash_is_not_a_proof(tmp_path: Path) -> None:
    case = next(case for case in load_cases() if case["id"] == "web-transfer-confirm")
    case = {
        **case,
        "numbers": [
            {
                "pattern": r"金额 (?P<value>[\d.]+) 元",
                "sql": "SELECT 'transactions:invented', 50000",
                "unit": "cents",
            }
        ],
    }
    report = run_benchmark(tmp_path, cases=[case])
    assert report["passed"] is False
    assert report["summary"]["money_trace_rate"] == {
        "numerator": 0,
        "denominator": 1,
        "rate": 0.0,
    }


def test_zero_denominators_are_na_and_failures_stay_visible() -> None:
    summary = summarize([])
    assert summary["attack_block_rate"] == {
        "numerator": 0,
        "denominator": 0,
        "rate": None,
    }


def test_yearly_totals_and_percentage_have_independent_source_proofs(tmp_path: Path) -> None:
    case = next(case for case in load_cases() if case["id"] == "web-bills-yearly")
    report = run_benchmark(tmp_path, cases=[case])
    assert report["passed"] is True
    proofs = report["cases"][0]["numeric_proofs"]
    assert any(proof["displayed"] == "+17.57%" for proof in proofs)
    percentage = next(proof for proof in proofs if proof["displayed"] == "+17.57%")
    assert len(percentage["sources"]) == 18
    assert all(proof["passed"] for proof in proofs)


@pytest.mark.parametrize("arm, expected", [("reviewer", 180.0), ("baseline", None)])
def test_strict_manifest_distinguishes_planner_and_reviewer_timeouts(
    arm: str, expected: float | None,
) -> None:
    from ai_banking_copilot.eval.web_benchmark import _metadata

    metadata = _metadata([], channel="llm-strict", arm=arm)
    assert metadata["timeout_s"] == 20.0
    assert metadata["reviewer_timeout_s"] == expected


class _AuditReviewer:
    """只替代模型；规划、路由及审计仍走真实实现。"""

    def __init__(self, *statuses: str, channel: str = "llm-strict", diagnostic: bool = False):
        self.statuses = iter(statuses)
        self.channel = channel
        self.diagnostic = diagnostic

    def review(self, request: Any) -> Any:
        from ai_banking_copilot.planning.reviewer import ReviewerFailure, ReviewReport, ReviewStatus

        status = next(self.statuses)
        if status == "unavailable":
            raise RuntimeError("PRIVATE_EXCEPTION_BODY")
        return ReviewReport(
            execution_id=request.execution_id,
            cfhash=request.cfhash,
            prompt_version=request.prompt_version,
            status=ReviewStatus(status),
            channel=self.channel,
            latency_ms=12.5,
            timeout_s=180,
            failure_code=ReviewerFailure.TIMEOUT if status == "manual" else None,
            error_type="ReadTimeout" if status == "manual" else None,
            http_status=200 if status == "manual" else None,
            diagnostic_only=self.diagnostic,
            findings=("PRIVATE_FINDING_BODY",),
            clarifications=("请选择账户",),
            error="PRIVATE_ERROR_BODY" if status != "allow" else None,
            model="PRIVATE_MODEL_BODY",
        )


def _inject_reviewer(monkeypatch: pytest.MonkeyPatch, reviewer: Any) -> None:
    from ai_banking_copilot.eval import web_benchmark

    original = web_benchmark.build_deps

    def configured(*args: Any, **kwargs: Any) -> Any:
        return replace(original(*args, **kwargs), reviewer=reviewer, reviewer_required=True)

    monkeypatch.setattr(web_benchmark, "build_deps", configured)


@pytest.mark.parametrize("status", ["clarify", "reject", "manual", "unavailable"])
def test_reviewer_observation_does_not_rescore_completed_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    _inject_reviewer(monkeypatch, _AuditReviewer(status))
    case = next(case for case in load_cases() if case["id"] == "web-bills-balance")
    report = run_benchmark(tmp_path, cases=[case])
    result = report["cases"][0]
    assert result["state"] == "completed"
    assert result["passed"] is True
    assert report["summary"]["task_success_rate"]["rate"] == 1
    reviews = result["reviewer_observations"]
    assert len(reviews) == 1  # 普通 PDP decision 不能当成另一轮审查。
    review = reviews[0]
    assert review["status"] == ("manual" if status == "unavailable" else status)
    assert review["channel"] == ("unavailable" if status == "unavailable" else "llm-strict")
    assert review["latency_ms"] == (0 if status == "unavailable" else 12.5)
    if status == "manual":
        assert review["failure_code"] == "timeout"
    if status == "unavailable":
        assert review["failure_code"] == "unavailable"
    serialized = json.dumps(report)
    assert "PRIVATE_" not in serialized
    group = "unavailable" if status == "unavailable" else "llm_strict"
    metric = f"reviewer_{group}_{review['status']}_rate"
    assert report["summary"][metric] == {"numerator": 1, "denominator": 1, "rate": 1}
    with (tmp_path / "summary.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = {row["metric"]: row for row in csv.DictReader(handle)}
    assert rows[metric]["numerator"] == "1"
    assert rows["reviewer_template_allow_rate"]["rate"] == "N/A"


@pytest.mark.parametrize("channel,diagnostic,group", [
    ("bypass", False, "bypass"),
    ("template", False, "template"),
    ("llm-strict", True, "diagnostic"),
])
def test_reviewer_channels_do_not_inflate_real_allow_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    channel: str, diagnostic: bool, group: str,
) -> None:
    _inject_reviewer(monkeypatch, _AuditReviewer("allow", channel=channel, diagnostic=diagnostic))
    case = next(case for case in load_cases() if case["id"] == "web-bills-balance")
    report = run_benchmark(tmp_path, cases=[case])
    assert report["summary"]["reviewer_llm_strict_allow_rate"] == {
        "numerator": 0, "denominator": 0, "rate": None,
    }
    assert report["summary"][f"reviewer_{group}_allow_rate"]["numerator"] == 1
    assert report["cases"][0]["reviewer_observations"][0]["diagnostic_only"] is diagnostic


def test_reviewer_clarification_keeps_both_audited_reviews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inject_reviewer(monkeypatch, _AuditReviewer("clarify", "allow"))
    case = next(case for case in load_cases() if case["id"] == "web-transfer-confirm")
    report = run_benchmark(tmp_path, cases=[{**case, "answers": {"account": "acc_savings"}}])
    result = report["cases"][0]
    assert result["passed"] is True
    assert [review["status"] for review in result["reviewer_observations"]] == ["clarify", "allow"]
    assert report["summary"]["reviewer_llm_strict_allow_rate"]["rate"] == 0.5


def test_no_reviewer_has_no_observation_or_allow_denominator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inject_reviewer(monkeypatch, None)
    case = next(case for case in load_cases() if case["id"] == "web-bills-balance")
    report = run_benchmark(tmp_path, cases=[case])
    assert report["cases"][0]["reviewer_observations"] == []
    assert summarize([])["reviewer_llm_strict_allow_rate"]["rate"] is None
    assert report["summary"]["reviewer_llm_strict_allow_rate"]["denominator"] == 0


def test_repeat_due_only_counts_actual_audited_reviews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inject_reviewer(monkeypatch, _AuditReviewer("allow"))
    case = next(case for case in load_cases() if case["id"] == "web-cross-birthday")
    report = run_benchmark(tmp_path, cases=[case])
    result = report["cases"][0]
    assert result["passed"] is True
    assert sum(item["endpoint"] == "/api/due" for item in result["trace"]) == 2
    # 重复检查已经完成的到期事项没有新计划，不能虚构第二次审查。
    assert len(result["reviewer_observations"]) == 1
    assert report["summary"]["reviewer_llm_strict_allow_rate"]["denominator"] == 1


def test_reviews_survive_business_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_banking_copilot.eval import web_benchmark

    _inject_reviewer(monkeypatch, _AuditReviewer("allow"))

    def failed_confirmation(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("模拟确认失败")

    monkeypatch.setattr(web_benchmark, "_confirm", failed_confirmation)
    case = next(case for case in load_cases() if case["id"] == "web-transfer-confirm")
    report = run_benchmark(tmp_path, cases=[case])
    result = report["cases"][0]
    assert result["passed"] is False
    assert result["state"] is None
    assert [review["status"] for review in result["reviewer_observations"]] == ["allow"]
    assert report["summary"]["task_success_rate"]["rate"] == 0
    assert report["summary"]["reviewer_llm_strict_allow_rate"]["rate"] == 1


@pytest.mark.parametrize("stage", ["case", "numeric_provenance"])
def test_benchmark_failure_summary_never_contains_exception_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    from ai_banking_copilot.eval import web_benchmark

    secret = "PRIVATE_API_KEY_AND_PROVIDER_RESPONSE"

    def failed(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(secret)

    monkeypatch.setattr(
        web_benchmark, "_prepare" if stage == "case" else "_numeric_candidates", failed,
    )
    case = next(case for case in load_cases() if case["id"] == "web-bills-balance")
    if stage == "numeric_provenance":
        monkeypatch.setattr(web_benchmark, "_provenance", lambda _deps: {})
        proofs = web_benchmark._numeric_proofs(["余额 1 元"], case, None, None)
        assert proofs[0]["failure"] == "numeric_provenance_error:RuntimeError"
        assert secret not in json.dumps(proofs)
    report = run_benchmark(tmp_path, cases=[case])
    assert report["cases"][0]["passed"] is False
    assert secret not in json.dumps(report)
    assert secret not in (tmp_path / "results.json").read_text(encoding="utf-8")


def test_observation_failure_still_closes_owned_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    from ai_banking_copilot.eval import web_benchmark

    original_build = web_benchmark.build_deps
    original_store = web_benchmark.Store
    stores = []
    handle = (tmp_path / "owned-resource.txt").open("w", encoding="utf-8")

    def configured(*args: Any, **kwargs: Any) -> Any:
        kwargs["resources"].enter_context(handle)
        return original_build(*args, **kwargs)

    def tracked_store(*args: Any, **kwargs: Any) -> Any:
        store = original_store(*args, **kwargs)
        stores.append(store)
        return store

    def failed_observations(deps: Any) -> Any:
        raise OSError("模拟审计读取失败")

    monkeypatch.setattr(web_benchmark, "build_deps", configured)
    monkeypatch.setattr(web_benchmark, "Store", tracked_store)
    monkeypatch.setattr(web_benchmark, "_reviewer_observations", failed_observations)
    case = next(case for case in load_cases() if case["id"] == "web-bills-balance")
    try:
        with pytest.raises(OSError, match="模拟审计读取失败"):
            run_benchmark(tmp_path / "benchmark", cases=[case])
        assert stores
        for store in stores:
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                store.query("SELECT 1")
        assert handle.closed
    finally:
        for store in stores:
            store.close()
        handle.close()


def test_unknown_audit_metadata_is_not_copied(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from ai_banking_copilot.eval.web_benchmark import _reviewer_observations
    from ai_banking_copilot.policy.audit import AuditEvent, AuditLog

    audit = AuditLog(tmp_path / "audit.jsonl")
    payload = {
        "review_status": "manual", "review_channel": "llm-strict",
        "review_failure": "PRIVATE_FAILURE_BODY", "review_error_type": "PRIVATE_ERROR_TYPE",
        "review_latency_ms": 9.25, "review_timeout_s": 180, "review_http_status": 503,
        "review_diagnostic_only": False, "review_model": "PRIVATE_MODEL_BODY",
        "review_findings": ["PRIVATE_FINDING_BODY"], "authorization": "PRIVATE_AUTH_BODY",
    }
    audit.append(AuditEvent("ignored-execution", "execution", payload, ""))
    audit.append(AuditEvent("review", "decision", payload, ""))
    reviews = _reviewer_observations(SimpleNamespace(audit=audit))
    assert len(reviews) == 1
    assert reviews[0]["audit_event_index"] == 1
    assert reviews[0]["failure_code"] == "unknown"
    assert "error_type" not in reviews[0]
    assert "http_status" not in reviews[0]
    assert "timeout_s" not in reviews[0]
    assert reviews[0]["latency_ms"] == 9.25
    assert "PRIVATE_" not in json.dumps(reviews)


def _user_utterance(content: str) -> str:
    """从带会话上下文的 planner user message 里取回【用户原话】。"""
    marker = "【用户原话】\n"
    if marker in content:
        return content.split(marker, 1)[1].split("\n\n", 1)[0]
    return content


def _fake_model_http(monkeypatch: pytest.MonkeyPatch, *, reviewer_failure: str | None = None,
                     clarify: bool = False) -> list[str]:
    """假客户端接替 I/O，保留原 transport、解析器、Web 与审计。"""
    import httpx

    calls: list[str] = []
    monkeypatch.setenv("AI_API_KEY", "offline-test-key")
    monkeypatch.setenv("AI_BASE_URL", "https://offline.invalid/chat/completions")
    for name in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def response(role: str, payload: Any) -> Any:
        calls.append(role)
        if role == "planner":
            text = _user_utterance(payload["messages"][1]["content"])
            action = "transfer.create" if "转" in text else "account.balance"
            content = {"intents": [{"action": action, "confidence": 1,
                                   "slots": {"span": text}, "evidence": text}]}
            usage = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
                     "completion_tokens_details": {"reasoning_tokens": 2}}
        else:
            if reviewer_failure == "timeout":
                raise httpx.ReadTimeout("PRIVATE_TIMEOUT_BODY")
            if reviewer_failure == "http":
                return httpx.Response(503, text="PRIVATE_HTTP_BODY")
            request = json.loads(payload["messages"][1]["content"])
            content = {key: request[key] for key in ("execution_id", "cfhash", "prompt_version")}
            content.update(status="clarify" if clarify and calls.count(role) == 1 else "allow",
                           findings=[], clarifications=[],
                           usage={"total_tokens": 999999})
            usage = {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(content, ensure_ascii=False)}}], "usage": usage})

    class Client:
        def __init__(self, **kwargs: Any):
            pass

        def post(self, url: str, *, json: Any, **kwargs: Any) -> Any:
            return response("planner", json)

    class AsyncClient:
        def __init__(self, **kwargs: Any):
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def post(self, url: str, *, json: Any, **kwargs: Any) -> Any:
            return response("reviewer", json)

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(httpx, "AsyncClient", AsyncClient)
    return calls


@pytest.mark.parametrize("arm,failure", [("baseline", None), ("reviewer", None),
                                         ("reviewer", "http"), ("reviewer", "timeout")])
def test_http_attempts_and_provider_usage_are_independent_of_business(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str, failure: str | None,
) -> None:
    case = next(case for case in load_cases() if case["id"] == "web-bills-balance")
    template = run_benchmark(tmp_path / "template", cases=[case])
    calls = _fake_model_http(monkeypatch, reviewer_failure=failure)
    report = run_benchmark(tmp_path / "strict", cases=[case], channel="llm-strict", arm=arm)
    observations = report["cases"][0]["model_calls"]
    assert [item["role"] for item in observations] == calls
    assert calls == (["planner"] if arm == "baseline" else ["planner", "reviewer"])
    assert all(set(item) == {"role", "http_status", "latency_ms", "failure_code", "usage",
                             "task_id", "attempt_no", "channel", "model", "prompt_version"}
               for item in observations)
    assert all(item["attempt_no"] == 1 and item["channel"] == "llm-strict"
               for item in observations)
    assert all(item["latency_ms"] >= 0 for item in observations)
    metadata = report["metadata"]
    assert metadata["model_call_count"] == len(calls)
    assert metadata["cost"] == "unknown"
    roles = metadata["model_calls_by_role"]
    assert roles["planner"]["http_attempts"] == 1
    assert roles["planner"]["usage"]["total_tokens"] == 13
    assert roles["reviewer"]["http_attempts"] == (arm == "reviewer")
    assert roles["reviewer"]["latency_ms"] == sum(
        item["latency_ms"] for item in observations if item["role"] == "reviewer")
    expected_total = None if failure else (37 if arm == "reviewer" else 13)
    assert metadata["usage"]["total_tokens"] == expected_total
    assert metadata["usage"]["completion_tokens_details.reasoning_tokens"] == (
        2 if arm == "baseline" else None)
    assert report["business_digest"] == template["business_digest"]
    assert report["cases"][0]["passed"] is True
    if failure:
        expected_failure = "http_error" if failure == "http" else "timeout"
        assert observations[-1]["failure_code"] == expected_failure
        assert observations[-1]["usage"] is None
    assert "PRIVATE_" not in json.dumps(report)
    saved = json.loads((tmp_path / "strict" / "results.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "strict" / "manifest.json").read_text(encoding="utf-8"))
    assert saved["metadata"] == manifest == metadata


def test_repeated_review_http_attempts_are_not_collapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_model_http(monkeypatch, clarify=True)
    case = next(case for case in load_cases() if case["id"] == "web-transfer-confirm")
    report = run_benchmark(tmp_path, cases=[{**case, "answers": {"account": "acc_savings"}}],
                           channel="llm-strict", arm="reviewer")
    assert calls == ["planner", "reviewer", "planner", "reviewer"]
    assert len(report["cases"][0]["model_calls"]) == 4
    assert len(report["cases"][0]["reviewer_observations"]) == 2
    assert report["metadata"]["usage"]["total_tokens"] == 74


def test_no_http_attempts_have_known_zero_count_and_no_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("AI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for channel in ("template", "llm-strict"):
        report = run_benchmark(tmp_path / channel, cases=[], channel=channel)
        assert report["metadata"]["model_call_count"] == 0
        assert report["metadata"]["usage"] is None
        assert report["metadata"]["model_calls_by_role"] == {
            role: {"http_attempts": 0, "latency_ms": 0, "usage": None}
            for role in ("planner", "reviewer")}
