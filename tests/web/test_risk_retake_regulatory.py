"""测评合规：2 次/日、8 次/12 个月、结果不一致再确认。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.risk_store import (
    RETAKES_PER_DAY,
    attempts_on,
    record_assessment_event,
    save_risk_assessment,
)
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.web.app import AppDeps, create_app

_LOW = {
    "q1_surplus": "q1_a",
    "q2_experience": "q2_a",
    "q3_drawdown": "q3_a",
    "q4_horizon": "q4_a",
    "q5_reaction": "q5_a",
}


def _client(deps: AppDeps) -> TestClient:
    return TestClient(create_app(deps))


def test_changed_result_requires_reconfirmation(deps: AppDeps, rw_store: Store) -> None:
    save_risk_assessment(rw_store, DEMO_USER_ID, "R3", DEFAULT_BOOKING_DATE)
    client = _client(deps)
    first = client.post("/api/risk-assessment", json={"answers": _LOW}).json()
    assert first["state"] == "needs_reconfirmation"
    assert first["previous_profile"] == "R3"
    assert attempts_on(rw_store, DEMO_USER_ID, deps.today) == 0

    confirmed = client.post(
        "/api/risk-assessment",
        json={"answers": _LOW, "confirm_change": True},
    ).json()
    assert confirmed["state"] == "updated"
    assert confirmed["assessment"]["risk_profile"] == "R1"
    assert attempts_on(rw_store, DEMO_USER_ID, deps.today) == 1


def test_daily_limit_is_two(deps: AppDeps, rw_store: Store) -> None:
    save_risk_assessment(rw_store, DEMO_USER_ID, "R1", DEFAULT_BOOKING_DATE)
    client = _client(deps)
    for _ in range(RETAKES_PER_DAY):
        assert client.post("/api/risk-assessment", json={"answers": _LOW}).status_code == 200
    blocked = client.post("/api/risk-assessment", json={"answers": _LOW})
    assert blocked.status_code == 429
    assert blocked.json()["retake"]["remaining_today"] == 0
    assert "第二十九条" in blocked.json()["detail"]


def test_twelve_month_limit_is_eight(deps: AppDeps, rw_store: Store) -> None:
    for _ in range(8):
        record_assessment_event(
            rw_store,
            DEMO_USER_ID,
            deps.today,
            "R1",
            0,
            source="questionnaire",
        )
    overview = _client(deps).get("/api/wealth/overview").json()
    assert overview["assessment"]["retake"]["used_12m"] == 8
    assert overview["assessment"]["retake"]["remaining_12m"] == 0

    blocked = _client(deps).post("/api/risk-assessment", json={"answers": _LOW})
    assert blocked.status_code == 429
    assert blocked.json()["retake"]["remaining_12m"] == 0
