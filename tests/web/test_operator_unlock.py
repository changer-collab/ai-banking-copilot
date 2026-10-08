"""运维解锁端点（README 裁决 #27）：**唯一**降级出口，必须 fail-closed 且留痕。

覆盖四类纪律：未配置口令 ⇒ 403；口令错 ⇒ 403；空理由 ⇒ 400；正确 ⇒ 解锁 + 审计留痕 + 链路完整。
"""

from dataclasses import replace

from fastapi.testclient import TestClient

from ai_banking_copilot.policy.audit import KIND_OPERATOR
from ai_banking_copilot.policy.breaker import BreakerTrigger
from ai_banking_copilot.web.app import AppDeps, create_app

TOKEN = "op-secret-token"
HEADERS = {"X-Operator-Token": TOKEN}
PATH = "/api/operator/breaker-unlock"


def _client(deps: AppDeps, token: str | None) -> TestClient:
    return TestClient(create_app(replace(deps, operator_token=token)))


def test_unlock_is_forbidden_when_no_token_is_configured(deps: AppDeps) -> None:
    """fail-closed：没配 `AIB_OPERATOR_TOKEN` ⇒ 一律 403，**不存在**「无口令也能解锁」的配置。"""
    deps.breaker.lock()
    response = _client(deps, None).post(PATH, json={"reason": "演示复位"}, headers=HEADERS)
    assert response.status_code == 403
    assert deps.breaker.is_open() is True


def test_unlock_rejects_a_wrong_token(deps: AppDeps) -> None:
    deps.breaker.lock()
    response = _client(deps, TOKEN).post(
        PATH, json={"reason": "演示复位"}, headers={"X-Operator-Token": "wrong"}
    )
    assert response.status_code == 403
    assert deps.breaker.is_open() is True


def test_unlock_requires_a_reason(deps: AppDeps) -> None:
    deps.breaker.lock()
    response = _client(deps, TOKEN).post(PATH, json={"reason": "   "}, headers=HEADERS)
    assert response.status_code == 400
    assert deps.breaker.is_open() is True


def test_unlock_is_the_only_downgrade_exit_and_it_is_audited(deps: AppDeps) -> None:
    """解锁生效 + 留痕：前后状态 / 触发项 / 理由都进审计链，且链仍完整。"""
    deps.breaker.trip(BreakerTrigger.INJECTION_DETECTED)
    assert deps.breaker.is_open() is True

    response = _client(deps, TOKEN).post(
        PATH, json={"reason": "误报，已人工核实"}, headers=HEADERS
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["state_before"] == "open"
    assert payload["state_after"] == "closed"
    assert payload["triggers_before"] == [BreakerTrigger.INJECTION_DETECTED.value]
    assert deps.breaker.is_open() is False

    operator_events = [event for event in deps.audit.events() if event.kind == KIND_OPERATOR]
    assert len(operator_events) == 1
    assert operator_events[0].payload["reason"] == "误报，已人工核实"
    assert operator_events[0].payload["state_before"] == "open"
    assert operator_events[0].payload["state_after"] == "closed"
    assert deps.audit.verify_chain() is True


def test_unlock_restores_writes_end_to_end(deps: AppDeps) -> None:
    """端到端：熔断打开 ⇒ 非纯查询转人工接管；运维解锁后同一动作可正常执行。"""
    client = _client(deps, TOKEN)
    deps.breaker.lock()

    blocked = client.post("/api/submit", json={"utterance": "转给李娜 100 元"}).json()
    refused = client.post(
        "/api/confirm",
        json={
            "card_id": blocked["card"]["card_id"],
            "frozen_hash": blocked["card"]["frozen_hash"],
        },
    ).json()
    assert refused["state"] == "taken_over"

    assert client.post(PATH, json={"reason": "演示复位"}, headers=HEADERS).status_code == 200

    again = client.post("/api/submit", json={"utterance": "转给李娜 100 元"}).json()
    done = client.post(
        "/api/confirm",
        json={
            "card_id": again["card"]["card_id"],
            "frozen_hash": again["card"]["frozen_hash"],
        },
    ).json()
    assert done["state"] == "completed"
