import json

import pytest

from ai_banking_copilot.planning.llm import (
    LlmIntentModel,
    LlmUnavailableError,
)


def test_injected_transport_is_used_without_network() -> None:
    requests: list[str] = []

    def _transport(request: str) -> str:
        requests.append(request)
        return json.dumps(
            {"intents": [{"action": "transfer.create", "confidence": 0.8, "evidence": "转给李娜"}]}
        )

    proposal = LlmIntentModel(transport=_transport).propose("转给李娜 500 元", {})
    assert [intent.action for intent in proposal.intents] == ["transfer.create"]
    assert proposal.intents[0].confidence == 0.8
    assert len(requests) == 1


def test_actions_outside_the_whitelist_are_dropped() -> None:
    def _transport(request: str) -> str:
        return json.dumps({"intents": [{"action": "exec_shell", "confidence": 0.9}]})

    assert LlmIntentModel(transport=_transport).propose("rm -rf /", {}).intents == ()


def test_malformed_response_raises_unavailable() -> None:
    def _transport(request: str) -> str:
        return "not-json"

    with pytest.raises(LlmUnavailableError, match="JSON"):
        LlmIntentModel(transport=_transport).propose("转给李娜 500 元", {})


def test_non_numeric_confidence_raises_unavailable() -> None:
    def _transport(request: str) -> str:
        return json.dumps({"intents": [{"action": "bills.monthly", "confidence": "高"}]})

    with pytest.raises(LlmUnavailableError, match="confidence"):
        LlmIntentModel(transport=_transport).propose("查账单", {})


def test_missing_credentials_raise_before_any_client_is_loaded(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    model = LlmIntentModel()
    assert model.loaded is False
    with pytest.raises(LlmUnavailableError, match="凭据"):
        model.propose("转给李娜 500 元", {})


def test_transport_is_injected_not_loaded_eagerly() -> None:
    def _transport(request: str) -> str:
        return json.dumps({"intents": []})

    assert LlmIntentModel().loaded is False
    assert LlmIntentModel(transport=_transport).loaded is True
