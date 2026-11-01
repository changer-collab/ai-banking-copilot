"""注入模型提案验证服务端边界；这些离线测试不判定真实模型的识别能力。"""

import json
from dataclasses import replace

import pytest

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.planning.llm import LlmIntentModel
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session, SessionState


def _user_utterance(request: str) -> str:
    """从带会话上下文的 user message 里取回【用户原话】原文。"""
    content = str(json.loads(request)["messages"][-1]["content"])
    marker = "【用户原话】\n"
    if marker in content:
        return content.split(marker, 1)[1].split("\n\n", 1)[0]
    return content


def proposed_model(action: str, confidence: float = 1.0) -> LlmIntentModel:
    def transport(request: str) -> str:
        text = _user_utterance(request)
        return json.dumps({"intents": [{
            "action": action, "confidence": confidence, "evidence": text,
            "slots": {
                "span": text, "user_id": "u_other", "account": "acc_current",
                "amount": 99999999, "year": 2099, "month": 1,
            },
        }]})

    return LlmIntentModel(transport=transport)


@pytest.mark.parametrize(
    "text, action, expected",
    [
        ("我花了多少钱", "bills.monthly", {"user_id": "u_demo", "year": 2026, "month": 9}),
        ("余额有多少", "account.balance", {"account_id": "acc_savings"}),
    ],
)
def test_query_without_amount_or_date_uses_server_facts(
    deps: AppDeps, rw_store: object, text: str, action: str, expected: dict[str, object],
) -> None:
    before = tuple(tuple(row) for row in rw_store.query("SELECT * FROM accounts"))
    view = Session(replace(deps, intent_model=proposed_model(action))).submit(
        text, selected={"account": "acc_savings"},
    )
    assert view.state is SessionState.AWAITING_CONFIRM
    assert view.clarifications == ()
    assert view.card is not None and view.card.level is Level.GREEN
    assert view.plan is not None
    node = next(node for node in view.plan.nodes if node.action == action)
    assert node.params == expected
    assert all(deps.registry.get(node.action).is_pure_query for node in view.plan.nodes)
    assert tuple(tuple(row) for row in rw_store.query("SELECT * FROM accounts")) == before
    assert not rw_store.query("SELECT * FROM transactions WHERE channel='agent'")
    assert deps.audit.verify_chain()


@pytest.mark.parametrize(
    "text, action, confidence, expected_state",
    [
        ("余额有多少", "account.balance", 0.1, SessionState.BLOCKED),
        ("运行脚本", "exec_shell", 1.0, SessionState.BLOCKED),
        ("转给李娜", "transfer.create", 1.0, SessionState.NEEDS_CLARIFICATION),
        ("转给李娜 100 元或者 200 元", "transfer.create", 1.0,
         SessionState.NEEDS_CLARIFICATION),
        ("不要转给李娜 100 元", "transfer.create", 1.0, SessionState.BLOCKED),
    ],
)
def test_proposal_cannot_bypass_missing_conflicting_or_negated_control_values(
    deps: AppDeps, rw_store: object, text: str, action: str, confidence: float,
    expected_state: SessionState,
) -> None:
    before = tuple(tuple(row) for row in rw_store.query("SELECT * FROM accounts"))
    view = Session(replace(deps, intent_model=proposed_model(action, confidence))).submit(
        text, selected={"account": "acc_savings"},
    )
    assert view.state is expected_state
    assert view.plan is None
    assert view.card is None
    assert view.decision is None
    assert tuple(tuple(row) for row in rw_store.query("SELECT * FROM accounts")) == before
    assert not rw_store.query("SELECT * FROM transactions WHERE channel='agent'")
