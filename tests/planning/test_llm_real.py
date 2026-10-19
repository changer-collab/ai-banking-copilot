import json

import pytest

from ai_banking_copilot.planning.llm import (
    LlmIntentModel,
    LlmUnavailableError,
)
from ai_banking_copilot.tools.catalog import build_default_registry


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


def test_request_explains_catalog_actions_and_requires_separate_source_spans() -> None:
    requests: list[dict[str, object]] = []

    def _transport(request: str) -> str:
        requests.append(json.loads(request))
        return json.dumps({"intents": []})

    utterance = "冻结 500 元，转给李娜 300 元"
    LlmIntentModel(transport=_transport).propose(utterance, {})

    messages = requests[0]["messages"]
    assert isinstance(messages, list)
    prompt = messages[0]["content"]
    registry = build_default_registry()
    for action in registry.actions():
        assert f"{action}：{registry.get(action).description}" in prompt
    assert '"slots":{"span":"原文连续片段"}' in prompt
    assert "逐字复制的连续片段" in prompt
    assert "请求词" in prompt and "对应金额或日期" in prompt
    assert "不得共享整句" in prompt and "不得使用重叠片段" in prompt
    assert "只登记预约时不要选择会立即扣款的动作" in prompt
    assert messages[1] == {"role": "user", "content": utterance}


def test_request_only_describes_the_selected_action_subset() -> None:
    requests: list[dict[str, object]] = []

    def _transport(request: str) -> str:
        requests.append(json.loads(request))
        return json.dumps({"intents": []})

    LlmIntentModel(transport=_transport, allow_actions=("account.balance",)).propose(
        "查余额", {}
    )

    messages = requests[0]["messages"]
    assert isinstance(messages, list)
    prompt = messages[0]["content"]
    registry = build_default_registry()
    assert registry.get("account.balance").description in prompt
    assert all(
        action not in prompt for action in registry.actions() if action != "account.balance"
    )


def test_request_does_not_invent_semantics_for_custom_allowed_action() -> None:
    requests: list[dict[str, object]] = []

    def _transport(request: str) -> str:
        requests.append(json.loads(request))
        return json.dumps({"intents": []})

    LlmIntentModel(transport=_transport, allow_actions=("custom.query",)).propose(
        "查自定义内容", {}
    )

    messages = requests[0]["messages"]
    assert isinstance(messages, list)
    assert "custom.query：未提供动作说明" in messages[0]["content"]
