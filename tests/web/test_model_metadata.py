from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from ai_banking_copilot.planning.call_observation import CallObservation
from ai_banking_copilot.planning.llm import FallbackIntentModel, LlmIntentModel
from ai_banking_copilot.planning.task_observations import TaskObservations
from ai_banking_copilot.web.app import AppDeps, create_app


def test_fallback_channel_and_zero_real_calls_are_visible(deps: AppDeps) -> None:
    configured = replace(deps, intent_model=FallbackIntentModel(LlmIntentModel(api_key="")))
    response = TestClient(create_app(configured)).post(
        "/api/submit", json={"utterance": "转给李娜200元"},
    ).json()
    assert response["model"]["planner_channel"] == "template_fallback"
    assert response["model"]["http_attempts"] == 0
    assert response["model"]["usage"] == {}


def test_observations_are_partitioned_and_drained_by_execution() -> None:
    records = TaskObservations()
    with records.scope("first"):
        records(CallObservation("planner", 200, 1, None, {"total_tokens": 7}))
    records(CallObservation("reviewer", 200, 2, None, {"total_tokens": 11}, task_id="second"))
    assert records.drain("first")[0]["usage"] == {"total_tokens": 7}
    assert records.drain("first") == []
    assert records.drain("second")[0]["task_id"] == "second"
