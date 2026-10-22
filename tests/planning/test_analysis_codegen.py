import json

import pytest

from ai_banking_copilot.planning.analysis_codegen import (
    AnalysisCodeGenerationError,
    AnalysisTask,
    generate_analysis_code,
    validate_analysis_code,
)
from ai_banking_copilot.planning.model_config import ModelConfig


def _completion(code: str) -> str:
    return json.dumps(
        {"choices": [{"message": {"content": code}}]},
    )


def test_real_codegen_receives_task_and_field_names_only() -> None:
    requests: list[dict[str, object]] = []

    def transport(request: str) -> str:
        requests.append(json.loads(request))
        return _completion("sum(facts)")

    code = generate_analysis_code(
        AnalysisTask.SUM,
        ("facts",),
        config=ModelConfig.from_env({"AI_API_KEY": "local-fake"}),
        transport=transport,
    )

    assert code == "sum(facts)"
    assert len(requests) == 1
    serialized = json.dumps(requests[0])
    assert "sum" in serialized
    assert "facts" in serialized
    assert "record_id" not in serialized
    assert "value" not in serialized


def test_codegen_rejects_code_outside_the_parent_task_contract() -> None:
    with pytest.raises(AnalysisCodeGenerationError):
        validate_analysis_code(
            "sum(facts) + 987654321",
            AnalysisTask.SUM,
            ("facts",),
        )


def test_codegen_rejects_oversized_expression_before_parsing() -> None:
    with pytest.raises(AnalysisCodeGenerationError, match="预算"):
        validate_analysis_code("(" * 8_193, AnalysisTask.SUM, ("facts",))


@pytest.mark.parametrize(
    "code",
    [
        "sum(other)",
        "avg(facts)",
        "sum(facts); yoy(current, prior)",
        "__import__('os')",
    ],
)
def test_codegen_rejects_wrong_fields_operators_and_statements(code: str) -> None:
    with pytest.raises(AnalysisCodeGenerationError):
        validate_analysis_code(code, AnalysisTask.SUM, ("facts",))


def test_disabled_llm_uses_deterministic_template_without_calling_transport() -> None:
    def transport(_: str) -> str:
        raise AssertionError("disabled code generation must not call the model")

    code = generate_analysis_code(
        AnalysisTask.YOY,
        ("current", "prior"),
        config=ModelConfig.from_env({"AIB_DISABLE_LLM": "1", "AI_API_KEY": "local-fake"}),
        transport=transport,
    )

    assert code == "yoy(sum(current), sum(prior))"


def test_codegen_rejects_yoy_without_aggregating_each_fact_field() -> None:
    with pytest.raises(AnalysisCodeGenerationError):
        validate_analysis_code(
            "yoy(current, prior)",
            AnalysisTask.YOY,
            ("current", "prior"),
        )
