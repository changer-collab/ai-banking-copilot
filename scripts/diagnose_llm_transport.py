"""分别诊断真实 Planner 与 Reviewer 通道，不执行银行动作。"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from ai_banking_copilot.contracts.plan import AnchorSource, IntentRef, Plan, PlanNode, cfhash
from ai_banking_copilot.planning.llm import (
    DEFAULT_LLM_ENDPOINT,
    DEFAULT_LLM_MODEL,
    LlmIntentModel,
    LlmUnavailableError,
)
from ai_banking_copilot.planning.reviewer import (
    ReviewerFailure,
    ReviewRequest,
    StrictPlanReviewer,
    build_reviewer_chat_payload,
)
from ai_banking_copilot.planning.transport_probe import (
    ProbeAttempt,
    ProbeClass,
    ProbeResult,
    probe_chat_completion,
)

_UTTERANCE = "查一下上月账单"
_ACTION = "bills.monthly"


def _review_request() -> ReviewRequest:
    plan = Plan(
        plan_id="transport-diagnostic",
        nodes=(
            PlanNode(
                node_id="n1",
                action=_ACTION,
                intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="a1"),
            ),
        ),
    )
    return ReviewRequest(
        execution_id="transport-diagnostic",
        plan=plan,
        cfhash=cfhash(plan.nodes),
        raw_text=_UTTERANCE,
        candidate_actions=(_ACTION,),
    )


def _public_result(result: ProbeResult, classification: str | None = None) -> dict[str, object]:
    public = result.public_dict()
    if classification is not None:
        public["classification"] = classification
    return public


def run_diagnostics(
    *,
    probe: Callable[..., ProbeResult] = probe_chat_completion,
) -> dict[str, object]:
    """用生产请求与解析器诊断两个真实通道，返回无正文的公开报告。"""
    key_name = next(
        (
            name
            for name in ("AI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY")
            if os.environ.get(name, "").strip()
        ),
        None,
    )
    api_key = os.environ[key_name].strip() if key_name else ""
    endpoint = os.environ.get("AI_BASE_URL", "").strip() or DEFAULT_LLM_ENDPOINT
    model = os.environ.get("AI_MODEL", "").strip() or DEFAULT_LLM_MODEL
    timeout_text = os.environ.get("AI_TIMEOUT_S", "").strip() or "20"
    try:
        timeout_s = float(timeout_text)
    except ValueError:
        timeout_s = None
    if timeout_s is not None and (not math.isfinite(timeout_s) or timeout_s <= 0):
        timeout_s = None

    parsed_endpoint = urlparse(endpoint)
    hostname = parsed_endpoint.hostname or ""
    provider = "dashscope" if hostname.endswith("dashscope.aliyuncs.com") else hostname
    report: dict[str, object] = {
        "run_at_utc": datetime.now(UTC).isoformat(),
        "provider": provider,
        "endpoint_hostname": hostname,
        "model": model,
        "timeout_s": timeout_s,
    }
    config_reason: str | None = None
    if not api_key:
        config_reason = "缺少模型 API Key"
    elif timeout_s is None:
        config_reason = "AI_TIMEOUT_S 必须为正数"
    elif parsed_endpoint.scheme not in ("http", "https") or not hostname:
        config_reason = "AI_BASE_URL 必须是带主机名的 HTTP(S) 地址"
    elif key_name == "DEEPSEEK_API_KEY" and provider == "dashscope":
        config_reason = "DEEPSEEK_API_KEY 与 DashScope 端点不匹配"
    elif key_name == "DASHSCOPE_API_KEY" and hostname == "api.deepseek.com":
        config_reason = "DASHSCOPE_API_KEY 与 DeepSeek 端点不匹配"
    if config_reason is not None:
        report["config_reason"] = config_reason
        for channel in ("planner", "reviewer"):
            result = ProbeResult(channel, ProbeClass.CONFIG_MISSING, ())
            report[channel] = _public_result(result)
        report["parser_checks"] = {"planner": False, "reviewer": False}
        report["protocol_checks"] = {"planner": None, "reviewer": None}
        report["reviewer_binding_check"] = None
        report["ready_for_smoke"] = False
        return report
    assert timeout_s is not None

    planner_result: ProbeResult | None = None
    planner_attempts: list[ProbeAttempt] = []

    def planner_transport(request: str) -> str:
        nonlocal planner_result
        planner_result = probe(
            channel="planner",
            endpoint=endpoint,
            api_key=api_key,
            payload=json.loads(request),
            timeout_s=timeout_s,
            max_attempts=1,
        )
        if planner_result.classification is not ProbeClass.OK:
            raise RuntimeError("planner probe stopped")
        return planner_result.model_text or ""

    planner_ok = False
    planner_class = "model_output_error"
    for number in (1, 2):
        try:
            proposal = LlmIntentModel(
                transport=planner_transport,
                api_key=api_key,
                model=model,
                endpoint=endpoint,
                timeout_s=timeout_s,
            ).propose(_UTTERANCE, {})
            planner_ok = bool(proposal.intents)
        except LlmUnavailableError:
            planner_ok = False
        assert planner_result is not None
        planner_class = (
            ("ok" if planner_ok else "model_output_error")
            if planner_result.classification is ProbeClass.OK
            else planner_result.classification.value
        )
        planner_attempts.extend(
            replace(item, number=number, classification=planner_class)
            for item in planner_result.attempts
        )
        if planner_class == "ok":
            break
    assert planner_result is not None
    report["planner"] = _public_result(
        replace(planner_result, attempts=tuple(planner_attempts)), planner_class
    )

    reviewer_result: ProbeResult | None = None
    reviewer_attempts: list[ProbeAttempt] = []

    def reviewer_transport(request: str) -> str:
        nonlocal reviewer_result
        reviewer_result = probe(
            channel="reviewer",
            endpoint=endpoint,
            api_key=api_key,
            payload=build_reviewer_chat_payload(request, model=model),
            timeout_s=timeout_s,
            max_attempts=1,
        )
        if reviewer_result.classification is not ProbeClass.OK:
            raise RuntimeError("reviewer probe stopped")
        return reviewer_result.raw_response or ""

    for number in (1, 2):
        review = StrictPlanReviewer(
            transport=reviewer_transport,
            api_key=api_key,
            model=model,
            endpoint=endpoint,
            timeout_s=timeout_s,
        ).review(_review_request())
        assert reviewer_result is not None
        reviewer_class = (
            ("ok" if review.failure_code is None else "protocol_error")
            if reviewer_result.classification is ProbeClass.OK
            else reviewer_result.classification.value
        )
        reviewer_attempts.extend(
            replace(item, number=number, classification=reviewer_class)
            for item in reviewer_result.attempts
        )
        if reviewer_class == "ok":
            break
    assert reviewer_result is not None
    reviewer_ok = review.failure_code is None
    report["reviewer"] = _public_result(
        replace(reviewer_result, attempts=tuple(reviewer_attempts)), reviewer_class
    )
    report["parser_checks"] = {"planner": planner_ok, "reviewer": reviewer_ok}
    reviewer_protocol: bool | None = None
    reviewer_binding: bool | None = None
    if reviewer_result.classification is ProbeClass.PROTOCOL_ERROR:
        reviewer_protocol = False
    elif reviewer_result.classification is ProbeClass.OK:
        reviewer_protocol = review.failure_code in (None, ReviewerFailure.BINDING_MISMATCH)
        if reviewer_protocol:
            reviewer_binding = review.failure_code is None
    report["protocol_checks"] = {
        "planner": planner_ok
        if planner_result.classification is ProbeClass.OK
        else (False if planner_result.classification is ProbeClass.PROTOCOL_ERROR else None),
        "reviewer": reviewer_protocol,
    }
    report["reviewer_binding_check"] = reviewer_binding
    report["ready_for_smoke"] = planner_ok and reviewer_ok
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="诊断 Planner 与 Reviewer 真实模型通道")
    parser.add_argument("--json-out", metavar="PATH", help="将公开报告写入工作区 runtime/ 下")
    args = parser.parse_args(argv)
    if args.json_out:
        root = Path(__file__).resolve().parents[1]
        runtime = root / "runtime"
        target = Path(args.json_out)
        target = (target if target.is_absolute() else root / target).resolve()
        if not target.is_relative_to(runtime.resolve()):
            print("--json-out 必须位于工作区 runtime/ 下", file=sys.stderr)
            return 2

    report = run_diagnostics()
    if args.json_out:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    planner = report["planner"]
    reviewer = report["reviewer"]
    assert isinstance(planner, dict) and isinstance(reviewer, dict)
    print(
        f"Planner：{planner['classification']}；Reviewer：{reviewer['classification']}；"
        f"可继续冒烟测试：{'是' if report['ready_for_smoke'] else '否'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
