"""理财意图（S2 第 ① 句）：「我要做风险测评」必须直接出 5 题问卷。

硬约束：不反问等级（等级由服务端算分）、不产生计划卡、零业务写入、不出澄清问题。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.scenarios.wealth_assessment import QUESTIONNAIRE
from ai_banking_copilot.web.app import AppDeps, create_app
from ai_banking_copilot.web.session import Session, SessionState


def _client(deps: AppDeps) -> TestClient:
    return TestClient(create_app(deps))


def _business_state(store: Store) -> tuple[int, int, int, int]:
    """业务写入快照：交易数 / 回执数 / 持仓合计 / 测评来源。"""
    transactions = store.query("SELECT COUNT(*) AS n FROM transactions")[0]["n"]
    receipts = store.query("SELECT COUNT(*) AS n FROM write_receipts")[0]["n"]
    holdings = store.query(
        "SELECT COALESCE(SUM(amount_cents), 0) AS n FROM wealth_holdings"
    )[0]["n"]
    sources = store.query(
        "SELECT source FROM risk_assessments WHERE user_id = ?", (DEMO_USER_ID,)
    )
    return int(transactions), int(receipts), int(holdings), len(sources)


@pytest.mark.parametrize(
    "utterance",
    ["我要做风险测评", "帮我重新测评", "做一个风险评测", "测一下风险承受能力"],
)
def test_assessment_request_returns_questionnaire(
    deps: AppDeps, utterance: str
) -> None:
    payload = _client(deps).post("/api/submit", json={"utterance": utterance}).json()
    assert payload["state"] == "idle", payload
    assert payload["card"] is None
    assert payload["clarifications"] == []

    questionnaire = payload["questionnaire"]
    assert len(questionnaire) == len(QUESTIONNAIRE) == 5
    assert [item["id"] for item in questionnaire] == [item.id for item in QUESTIONNAIRE]
    for item in questionnaire:
        assert set(item) == {"id", "prompt", "options"}
        for option in item["options"]:
            assert set(option) == {"id", "label"}
    # 题面直接带出，用户不必再找入口；等级由系统算分，绝不反问用户等级
    for question in QUESTIONNAIRE:
        assert question.prompt in payload["message"]
    assert "风险等级" not in payload["message"]


def test_assessment_request_with_a_second_clause_still_gets_the_questionnaire(
    deps: AppDeps,
) -> None:
    """「我想先做个风险测评，再买理财」：先出问卷，并把后半句挂回来（不追问金额）。"""
    payload = _client(deps).post(
        "/api/submit", json={"utterance": "我想先做个风险测评，再买理财"}
    ).json()
    assert payload["state"] == "idle", payload
    assert payload["card"] is None
    assert payload["clarifications"] == []
    questionnaire = payload["questionnaire"]
    assert len(questionnaire) == len(QUESTIONNAIRE) == 5
    # 后半句不静默丢掉：话术里明确「测完再把买的部分发我一次」
    assert "再发我一次" in payload["message"]


def test_assessment_request_never_asks_for_a_level(deps: AppDeps) -> None:
    view = Session(deps).submit("我要做风险测评")
    assert view.state is SessionState.IDLE
    assert view.questions == ()
    assert view.clarifications == ()
    assert view.card is None and view.plan is None
    assert all("等级" not in prompt for prompt in view.questions)


def test_assessment_request_has_no_business_writes(
    deps: AppDeps, rw_store: Store
) -> None:
    before = _business_state(rw_store)
    _submit = _client(deps).post(
        "/api/submit", json={"utterance": "我要做风险测评"}
    ).json()
    assert _submit["questionnaire"]
    assert _business_state(rw_store) == before


def test_plain_query_is_not_mistaken_for_assessment_request(deps: AppDeps) -> None:
    payload = _client(deps).post(
        "/api/submit", json={"utterance": "看看理财产品"}
    ).json()
    assert payload["questionnaire"] == []
    assert payload["state"] == "awaiting_confirm"
