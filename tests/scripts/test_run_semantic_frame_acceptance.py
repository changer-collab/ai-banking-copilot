import json
import runpy

from ai_banking_copilot.eval.semantic_frame_cases import SEMANTIC_FRAME_CASES
from ai_banking_copilot.planning.llm import LlmIntentModel

_SCRIPT = runpy.run_path("scripts/run_semantic_frame_acceptance.py")
run_suite = _SCRIPT["run_suite"]


def test_semantic_acceptance_reports_each_case_without_exposing_raw_model_text() -> None:
    def transport(_request: str) -> str:
        case = SEMANTIC_FRAME_CASES[0]
        return json.dumps({"frames": [case.frame_payload()]})

    report = run_suite(LlmIntentModel(transport=transport), cases=SEMANTIC_FRAME_CASES[:1])

    assert report["summary"]["total"] == 1
    assert report["cases"][0]["passed"] is True
    assert "raw" not in report["cases"][0]
    assert "\u67e5\u4f59\u989d" not in json.dumps(report, ensure_ascii=False)
