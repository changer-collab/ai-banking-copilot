"""Run the fixed semantic-frame suite against the configured real planner.

The output is intentionally evidence-oriented: no API key, raw model text, or
user utterance is written to disk.  A failure remains in the report and is not
replaced by template output.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from ai_banking_copilot.__main__ import load_env_file
from ai_banking_copilot.contracts.security import detect_injection
from ai_banking_copilot.eval.semantic_frame_cases import (
    SEMANTIC_FRAME_CASES,
    SemanticFrameCase,
)
from ai_banking_copilot.planning.llm import IntentModel, LlmIntentModel, LlmUnavailableError
from ai_banking_copilot.planning.model_config import ModelConfig


def _frame_summary(frame: object) -> dict[str, object]:
    return {
        "domain": getattr(frame, "domain", ""),
        "operation": getattr(frame, "operation", ""),
        "mode": getattr(frame, "mode", ""),
        "roles": sorted(
            value.role for value in getattr(frame, "roles", {}).values()
        ),
    }


def run_suite(
    model: IntentModel,
    *,
    cases: Sequence[SemanticFrameCase] = SEMANTIC_FRAME_CASES,
) -> dict[str, Any]:
    """Run one model call per executable case and preserve all failures."""
    results: list[dict[str, object]] = []
    for case in cases:
        started = perf_counter()
        expected_roles = sorted(role for _, role, _ in case.roles.values())
        result: dict[str, object] = {
            "case_id": case.case_id,
            "expected_action": case.expected_action,
            "expected_disposition": case.expected_disposition,
            "expected_roles": expected_roles,
            "observed_actions": [],
            "observed_frames": [],
            "channel": None,
            "passed": False,
            "failure": None,
        }
        if case.expected_action is None:
            result["passed"] = detect_injection(case.utterance)
            result["failure"] = None if result["passed"] else "policy_guard_not_triggered"
        else:
            try:
                proposal = model.propose(case.utterance, {})
                actions = [intent.action for intent in proposal.intents]
                frames = [_frame_summary(frame) for frame in proposal.frames]
                role_types = sorted(
                    role for frame in frames for role in frame["roles"]  # type: ignore[index]
                )
                result["observed_actions"] = actions
                result["observed_frames"] = frames
                result["channel"] = proposal.channel
                result["passed"] = (
                    case.expected_action in actions
                    and all(role in role_types for role in expected_roles)
                )
                if not result["passed"]:
                    result["failure"] = "semantic_mapping_mismatch"
            except LlmUnavailableError as error:
                result["failure"] = f"planner_unavailable:{type(error).__name__}"
            except Exception as error:  # pragma: no cover - defensive script boundary
                result["failure"] = f"planner_error:{type(error).__name__}"
        result["latency_ms"] = round((perf_counter() - started) * 1000)
        results.append(result)
    passed = sum(item["passed"] is True for item in results)
    return {
        "run_at_utc": datetime.now(UTC).isoformat(),
        "suite": "semantic-frame-v1",
        "scope": "self-built fixed regression suite; not a blind benchmark",
        "summary": {"passed": passed, "total": len(results), "rate": passed / len(results)},
        "cases": results,
    }


def main(argv: Sequence[str] | None = None) -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-out",
        type=Path,
        default=Path("runtime/semantic-frame-acceptance.json"),
    )
    args = parser.parse_args(argv)
    target = args.json_out.resolve()
    runtime = (Path.cwd() / "runtime").resolve()
    if not target.is_relative_to(runtime):
        print("--json-out must be under runtime/", file=sys.stderr)
        return 2
    config = ModelConfig.from_env()
    report = run_suite(LlmIntentModel(config=config))
    report["model_config"] = config.public_metadata()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = report["summary"]
    print(f"semantic-frame-v1: {summary['passed']}/{summary['total']}")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
