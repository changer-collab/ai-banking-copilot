"""风险测评事实 → 场景层 `RiskAssessment` 的转换单点（未识别⇒None，fail-closed）。"""

from __future__ import annotations

from datetime import date

from ai_banking_copilot.gateway.risk_store import RiskAssessmentRecord
from ai_banking_copilot.scenarios.wealth import RiskAssessment, to_risk_assessment


def test_converts_record_to_scenario_assessment() -> None:
    record = RiskAssessmentRecord(
        user_id="u_demo", risk_profile="R3", assessed_on=date(2026, 9, 1), valid_days=365
    )
    converted = to_risk_assessment(record)
    assert converted == RiskAssessment(
        user_id="u_demo",
        risk_profile="R3",
        assessed_on=date(2026, 9, 1),
        valid_days=365,
        source="seed",
    )


def test_passthrough_existing_assessment() -> None:
    assessment = RiskAssessment(
        user_id="u_demo", risk_profile="R2", assessed_on=date(2026, 9, 1), valid_days=365
    )
    assert to_risk_assessment(assessment) is assessment


def test_rejects_unrecognized_object_and_bad_level() -> None:
    assert to_risk_assessment(None) is None
    assert to_risk_assessment(object()) is None
    bad = RiskAssessmentRecord(
        user_id="u_demo", risk_profile="R9", assessed_on=date(2026, 9, 1), valid_days=365
    )
    assert to_risk_assessment(bad) is None
