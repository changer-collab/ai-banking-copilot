"""任务 3 验收（web 层）：推荐只读、硬筛选不回流、交易边界不可绕过。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from fastapi.testclient import TestClient

from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.risk_store import save_risk_assessment
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.scenarios.wealth_assessment import MAX_SCORE, QUESTIONNAIRE
from ai_banking_copilot.web.app import AppDeps, create_app
from ai_banking_copilot.web.session import Session, SessionState, SessionStateError

_SEED_SOURCE_LABEL = "演示 seed 数据（非真实测评）"


def _client(deps: AppDeps) -> TestClient:
    return TestClient(create_app(deps))


def _business_state(store: Store) -> tuple[int, int, int]:
    """业务写入快照：交易数 / 回执数 / 账户余额合计（推荐查询不得改变它）。"""
    transactions = store.query("SELECT COUNT(*) AS n FROM transactions")[0]["n"]
    receipts = store.query("SELECT COUNT(*) AS n FROM write_receipts")[0]["n"]
    balances = store.query("SELECT COALESCE(SUM(balance_cents), 0) AS n FROM accounts")[0]["n"]
    return int(transactions), int(receipts), int(balances)


def _submit(client: TestClient, utterance: str, **extra: object) -> dict[str, object]:
    response = client.post("/api/submit", json={"utterance": utterance, **extra})
    assert response.status_code == 200
    return response.json()


def _confirm(client: TestClient, submitted: dict[str, object]) -> dict[str, object]:
    card = submitted["card"]
    assert isinstance(card, dict)
    response = client.post(
        "/api/confirm",
        json={"card_id": card["card_id"], "frozen_hash": card["frozen_hash"]},
    )
    assert response.status_code == 200
    return response.json()


def _card_text(submitted: dict[str, object]) -> str:
    card = submitted["card"]
    assert isinstance(card, dict)
    return "\n".join(str(line) for line in card["lines"])


def _message(result: dict[str, object]) -> str:
    results = result["results"]
    assert isinstance(results, list) and results
    return str(results[0]["message"])


# 3. 推荐意图不再被 H6 整句误拒；收益承诺仍被拦
def test_recommendation_intent_is_not_rejected_by_yield_guard(deps: AppDeps) -> None:
    submitted = _submit(_client(deps), "推荐理财产品")
    assert submitted["state"] == "awaiting_confirm"
    text = _card_text(submitted)
    assert "活期理财篮子" in text
    assert "成长精选 C" not in text


def test_yield_promise_is_still_blocked(deps: AppDeps) -> None:
    submitted = _submit(_client(deps), "推荐收益最高的理财")
    assert submitted["state"] == "blocked"
    assert submitted["card"] is None


# 4. 推荐查询零业务写入
def test_recommendation_query_has_no_business_writes(deps: AppDeps, rw_store: Store) -> None:
    before = _business_state(rw_store)
    client = _client(deps)
    submitted = _submit(client, "比较几款理财产品")
    assert submitted["state"] == "awaiting_confirm"
    result = _confirm(client, submitted)
    assert result["state"] == "completed"
    assert "合格候选" in _message(result)
    assert _business_state(rw_store) == before


# 1 + 5. R2：计划卡与执行结果都只含 R1/R2 产品
def test_r2_recommendation_excludes_higher_risk_products(
    deps: AppDeps, rw_store: Store
) -> None:
    save_risk_assessment(rw_store, DEMO_USER_ID, "R2", DEFAULT_BOOKING_DATE)
    client = _client(deps)
    submitted = _submit(client, "哪款适合我")
    text = _card_text(submitted)
    assert "活期理财篮子" in text
    assert "稳利债券 A" in text
    assert "均衡配置 B" not in text
    assert "成长精选 C" not in text
    message = _message(_confirm(client, submitted))
    assert "合格候选 2 只" in message
    assert "wp_mix" not in message
    assert "wp_equity" not in message
    assert "用户自助录入" in message


# 2. 无测评 / 测评过期：BLOCKED，且不默认任何等级
def test_missing_assessment_blocks_recommendation(deps: AppDeps) -> None:
    view = Session(replace(deps, risk=None)).submit("推荐理财产品")
    assert view.state is SessionState.BLOCKED
    assert view.card is None
    assert "风险测评" in view.message


def test_unassessed_source_never_defaults_to_high_risk(deps: AppDeps) -> None:
    class _NoAssessment:
        def assessment_for(self, user_id: str) -> object | None:
            return None

    view = Session(replace(deps, risk=_NoAssessment())).submit("推荐理财产品")
    assert view.state is SessionState.BLOCKED
    assert view.card is None
    assert "风险测评" in view.message


def test_expired_assessment_blocks_recommendation(deps: AppDeps, rw_store: Store) -> None:
    save_risk_assessment(rw_store, DEMO_USER_ID, "R4", date(2024, 1, 1), valid_days=30)
    view = Session(deps).submit("推荐理财产品")
    assert view.state is SessionState.BLOCKED
    assert "过期" in view.message


# 8. 购买/申购不可绕过独立确认与适当性校验
def test_subscribe_stops_at_card_without_writes(deps: AppDeps, rw_store: Store) -> None:
    before = _business_state(rw_store)
    submitted = _submit(_client(deps), "申购 2000 元理财", selected={"account": "acc_savings"})
    assert submitted["state"] == "awaiting_confirm"
    assert _business_state(rw_store) == before


def test_natural_language_confirmation_cannot_execute(deps: AppDeps, rw_store: Store) -> None:
    before = _business_state(rw_store)
    session = Session(deps)
    view = session.submit("申购 2000 元理财", selected={"account": "acc_savings"})
    assert view.card is not None
    card = view.card
    again = session.submit("确认")
    assert again.state is not SessionState.COMPLETED
    with pytest.raises(SessionStateError):
        session.confirm(card.card_id, frozen_hash=card.frozen_hash)
    assert _business_state(rw_store) == before


def test_r2_cannot_subscribe_ineligible_product(deps: AppDeps, rw_store: Store) -> None:
    save_risk_assessment(rw_store, DEMO_USER_ID, "R2", DEFAULT_BOOKING_DATE)
    before = _business_state(rw_store)
    view = Session(deps).submit(
        "申购 20000 元理财",
        selected={"account": "acc_savings", "product": "wp_equity"},
    )
    assert view.state is SessionState.BLOCKED
    assert view.card is None
    assert "风险等级不匹配" in view.message
    assert _business_state(rw_store) == before


# 风险偏好真实录入 / 读取 / 更新接口（seed 必须如实标注）
def test_risk_assessment_get_reports_seed_source(deps: AppDeps) -> None:
    payload = _client(deps).get("/api/risk-assessment").json()
    assert payload["state"] == "valid"
    assert payload["risk_profile"] == "R3"
    assert payload["source"] == "seed"
    assert payload["source_label"] == _SEED_SOURCE_LABEL


def test_risk_assessment_post_records_user_input(deps: AppDeps) -> None:
    payload = _client(deps).post("/api/risk-assessment", json={"risk_profile": "r2"}).json()
    assert payload["state"] == "updated"
    assessment = payload["assessment"]
    assert assessment["risk_profile"] == "R2"
    assert assessment["source"] == "user_input"
    assert assessment["source_label"] == "用户自助录入"
    assert assessment["assessed_on"] == DEFAULT_BOOKING_DATE.isoformat()


def test_risk_assessment_post_rejects_unknown_level(deps: AppDeps) -> None:
    response = _client(deps).post("/api/risk-assessment", json={"risk_profile": "R9"})
    assert response.status_code == 400


def test_risk_assessment_post_requires_writer(deps: AppDeps) -> None:
    client = _client(replace(deps, risk_writer=None))
    assert client.post("/api/risk-assessment", json={"risk_profile": "R2"}).status_code == 404


def test_risk_assessment_post_cannot_forge_identity(deps: AppDeps, rw_store: Store) -> None:
    response = _client(deps).post(
        "/api/risk-assessment", json={"risk_profile": "R2", "user_id": "u_attacker"}
    )
    assert response.status_code == 200
    rows = rw_store.query("SELECT user_id FROM risk_assessments ORDER BY user_id")
    assert [row["user_id"] for row in rows] == [DEMO_USER_ID]


# 风险测评问卷（P0-5）：等级由服务端评分算出，落库 source='questionnaire'
def _answers(option_index: int) -> dict[str, str]:
    return {question.id: question.options[option_index].id for question in QUESTIONNAIRE}


def test_risk_assessment_get_exposes_questionnaire(deps: AppDeps) -> None:
    payload = _client(deps).get("/api/risk-assessment").json()
    questionnaire = payload["questionnaire"]
    assert len(questionnaire) == len(QUESTIONNAIRE)
    assert questionnaire[0]["id"] == QUESTIONNAIRE[0].id
    assert "score" not in questionnaire[0]["options"][0]


def test_risk_assessment_questionnaire_records_computed_profile(
    deps: AppDeps, rw_store: Store
) -> None:
    payload = _client(deps).post(
        "/api/risk-assessment", json={"answers": _answers(3)}
    ).json()
    assessment = payload["assessment"]
    assert assessment["risk_profile"] == "R5"
    assert assessment["source"] == "questionnaire"
    assert assessment["source_label"] == "风险测评问卷"
    assert payload["result"]["score"] == MAX_SCORE
    rows = rw_store.query("SELECT source FROM risk_assessments WHERE user_id = ?", (DEMO_USER_ID,))
    assert rows[0]["source"] == "questionnaire"


def test_risk_assessment_questionnaire_is_stable_across_runs(deps: AppDeps) -> None:
    client = _client(deps)
    first = client.post("/api/risk-assessment", json={"answers": _answers(1)}).json()
    second = client.post("/api/risk-assessment", json={"answers": _answers(1)}).json()
    assert first["result"] == second["result"]


def test_risk_assessment_questionnaire_rejects_self_filled_level(deps: AppDeps) -> None:
    response = _client(deps).post(
        "/api/risk-assessment", json={"answers": _answers(3), "risk_profile": "R1"}
    )
    assert response.status_code == 400


def test_risk_assessment_questionnaire_rejects_incomplete_answers(deps: AppDeps) -> None:
    incomplete = _answers(3)
    incomplete.pop(QUESTIONNAIRE[0].id)
    response = _client(deps).post("/api/risk-assessment", json={"answers": incomplete})
    assert response.status_code == 400
