"""测评每日次数（实测 WEA-P08③）。

口径依据《金融机构产品适当性管理办法》第二十九条：同一业务日最多 2 次、
十二个月累计最多 8 次；演示环境按模拟业务日计数。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from fastapi.testclient import TestClient

from ai_banking_copilot.gateway.risk_store import RETAKES_PER_DAY, attempts_on
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.web.app import AppDeps, create_app

_ANSWERS = {
    "q1_surplus": "q1_c",
    "q2_experience": "q2_c",
    "q3_drawdown": "q3_c",
    "q4_horizon": "q4_c",
    "q5_reaction": "q5_c",
}


def _client(deps: AppDeps) -> TestClient:
    return TestClient(create_app(deps))


def test_retake_quota_is_reported_on_overview(deps: AppDeps) -> None:
    payload = _client(deps).get("/api/wealth/overview").json()
    quota = payload["assessment"]["retake"]

    assert quota["limit_per_day"] == RETAKES_PER_DAY
    assert quota["used_today"] == 0
    assert quota["remaining_today"] == RETAKES_PER_DAY
    assert quota["basis"] == "regulatory"
    assert quota["limit_per_12m"] == 8
    assert quota["next_available_on"] == (deps.today + timedelta(days=1)).isoformat()


def test_questionnaire_is_capped_per_business_day(deps: AppDeps) -> None:
    client = _client(deps)
    for _ in range(RETAKES_PER_DAY):
        response = client.post(
            "/api/risk-assessment", json={"answers": _ANSWERS, "confirm_change": True}
        )
        assert response.status_code == 200, response.text

    blocked = client.post(
        "/api/risk-assessment", json={"answers": _ANSWERS, "confirm_change": True}
    )

    assert blocked.status_code == 429
    body = blocked.json()
    assert body["retake"]["remaining_today"] == 0
    assert "第二十九条" in body["detail"]
    # 超限那次不得把次数继续累加
    assert body["retake"]["used_today"] == RETAKES_PER_DAY


def test_attempts_reset_on_the_next_business_day(deps: AppDeps, rw_store: Store) -> None:
    client = _client(deps)
    for _ in range(RETAKES_PER_DAY):
        client.post("/api/risk-assessment", json={"answers": _ANSWERS, "confirm_change": True})

    assert attempts_on(rw_store, deps.user_id, deps.today) == RETAKES_PER_DAY

    tomorrow = replace(deps, today=deps.today + timedelta(days=1))
    assert _client(tomorrow).post(
        "/api/risk-assessment", json={"answers": _ANSWERS, "confirm_change": True}
    ).status_code == 200
    assert attempts_on(rw_store, deps.user_id, tomorrow.today) == 1


def test_direct_input_path_does_not_consume_quota(deps: AppDeps) -> None:
    """`risk_profile` 直录是兼容路径，不是问卷，不计次。"""
    client = _client(deps)
    for _ in range(RETAKES_PER_DAY + 1):
        assert client.post("/api/risk-assessment", json={"risk_profile": "R2"}).status_code == 200

    payload = client.get("/api/wealth/overview").json()
    assert payload["assessment"]["retake"]["used_today"] == 0
