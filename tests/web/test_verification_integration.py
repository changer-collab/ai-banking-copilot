from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from fastapi.testclient import TestClient

from ai_banking_copilot.policy.verification import (
    DeliveryResult,
    VerificationBinding,
    VerificationError,
    VerificationMode,
)
from ai_banking_copilot.web.app import AppDeps, create_app
from ai_banking_copilot.web.session import Session


class _ExternalProvider:
    mode = VerificationMode.EXTERNAL

    def __init__(self, *, fail: bool = False) -> None:
        self.code: str | None = None
        self.fail = fail

    def deliver(
        self, binding: VerificationBinding, challenge_id: str,
        code: str, expires_at: datetime,
    ) -> DeliveryResult:
        if self.fail:
            raise VerificationError("delivery_failed")
        self.code = code
        return DeliveryResult(VerificationMode.EXTERNAL, "external", "****8000", "ref:test")


def test_external_api_omits_code_and_binds_session(deps: AppDeps) -> None:
    provider = _ExternalProvider()
    configured = replace(
        deps, verification_provider=provider, verification_key=b"test-key",
        verification_strict=True,
    )
    client = TestClient(create_app(configured))
    submitted = client.post(
        "/api/submit", json={"session_id": "one", "utterance": "转给李娜 1500 元"},
    ).json()
    prompt = client.post("/api/verify/request", json={"session_id": "one"})
    assert prompt.status_code == 200
    assert prompt.json()["mode"] == "external"
    assert "code" not in prompt.json()
    assert provider.code is not None
    confirmed = client.post("/api/confirm", json={
        "session_id": "one", "card_id": submitted["card"]["card_id"],
        "frozen_hash": submitted["card"]["frozen_hash"],
        "verification_code": provider.code,
    })
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == "completed"
    events = [event.payload for event in configured.audit.events()
              if event.payload.get("kind") == "verification"]
    assert {event["status"] for event in events} >= {"requested", "delivered", "consumed"}
    assert all(event["session_id"] == "one" for event in events)
    assert provider.code not in repr(events)
    assert "code_digest" not in repr(events)


def test_strict_demo_and_failed_delivery_do_not_create_active_challenge(deps: AppDeps) -> None:
    for configured in (
        replace(deps, verification_strict=True),
        replace(deps, verification_provider=_ExternalProvider(fail=True),
                verification_key=b"test-key", verification_strict=True),
    ):
        session = Session(configured, session_id="strict")
        submitted = session.submit("转给李娜 1500 元")
        assert submitted.card is not None
        client = TestClient(create_app(configured))
        card = client.post("/api/submit", json={"utterance": "转给李娜 1500 元"}).json()
        failed = client.post("/api/verify/request", json={})
        assert failed.status_code == 409
        assert "code" not in failed.json()
        denied = client.post("/api/confirm", json={
            "card_id": card["card"]["card_id"],
            "frozen_hash": card["card"]["frozen_hash"], "verification_code": "123456",
        })
        assert denied.status_code == 409
