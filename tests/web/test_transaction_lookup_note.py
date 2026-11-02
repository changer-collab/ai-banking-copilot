"""单笔交易核实：明确回答「没查到」，不拿大额扫描结果冒充结论。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app
from ai_banking_copilot.web.session import Session


def test_fraud_question_gets_a_deterministic_not_found_answer(deps: AppDeps) -> None:
    """「上周三星巴克128元是不是盗刷」⇒ 上周区间 + 未查询到该笔交易。"""
    view = Session(deps).submit("上周三星巴克128元是不是盗刷?")
    assert view.card is not None
    assert view.answer_note is not None
    assert "未查询到" in view.answer_note
    assert "128.00" in view.answer_note
    assert "2026-10-19~2026-10-25" in view.answer_note
    assert "均不匹配" in view.answer_note
    assert "已核实" not in view.answer_note
    line = "\n".join(view.card.lines)
    assert "2026-10-19~2026-10-25" in line


def test_submit_payload_carries_the_answer_note(deps: AppDeps) -> None:
    """答复随 `/api/submit` 下发，前端据此在计划卡上方展示。"""
    client = TestClient(create_app(deps))
    data = client.post(
        "/api/submit", json={"utterance": "上周三星巴克128元是不是盗刷?"}
    ).json()
    assert data["state"] == "awaiting_confirm"
    assert "未查询到" in str(data["answer_note"])


def test_plain_statement_query_has_no_lookup_note(deps: AppDeps) -> None:
    """普通交易记录查询不追加「某笔核实」答复，避免每张查询卡都挂一句没查到。"""
    view = Session(deps).submit("查一下我的交易记录")
    assert view.card is not None
    assert view.answer_note is None
