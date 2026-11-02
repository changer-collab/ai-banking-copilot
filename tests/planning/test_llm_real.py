import json
from types import SimpleNamespace

import httpx
import pytest

from ai_banking_copilot.planning.llm import (
    FallbackIntentModel,
    LlmIntentModel,
    LlmUnavailableError,
)
from ai_banking_copilot.tools.catalog import build_default_registry


@pytest.mark.parametrize(("status", "error", "body", "code"), [
    (200, None, json.dumps({
        "usage": {"total_tokens": 7},
        "choices": [{"message": {"content": '{"intents":[]}'}}],
    }), None),
    (200, None, "bad json", None),
    (503, None, "secret provider error", "http_error"),
    (None, httpx.ReadTimeout("secret timeout"), None, "timeout"),
    (None, httpx.ConnectError("secret connection"), None, "transport_error"),
])
def test_actual_planner_http_attempt_is_observed(monkeypatch, status, error, body, code):
    records = []
    calls = []
    class Client:
        def __init__(self, *, timeout):
            assert timeout == 20.0
        def post(self, endpoint, *, json, headers, timeout):
            calls.append(json)
            assert timeout == 20.0 and headers == {"Authorization": "Bearer local-fake"}
            if error is not None:
                raise error
            return SimpleNamespace(status_code=status, text=body)
    monkeypatch.setattr(
        "ai_banking_copilot.planning.llm.importlib.import_module",
        lambda name: SimpleNamespace(Client=Client),
    )
    model = LlmIntentModel(api_key="local-fake", observer=records.append)
    if error is not None or status != 200 or body == "bad json":
        with pytest.raises(LlmUnavailableError):
            model.propose("查余额", {})
    else:
        assert model.propose("查余额", {}).intents == ()
    assert len(calls) == len(records) == 1
    assert records[0].http_status == status
    assert records[0].failure_code == code
    expected_usage = {"total_tokens": 7} if status == 200 and body != "bad json" else None
    assert records[0].usage == expected_usage


def test_planner_injected_transport_and_missing_credentials_emit_nothing(monkeypatch):
    records = []
    LlmIntentModel(
        transport=lambda request: '{"intents":[]}', observer=records.append,
    ).propose("查余额", {})
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(LlmUnavailableError, match="凭据"):
        LlmIntentModel(observer=records.append).propose("查余额", {})
    assert records == []


@pytest.mark.parametrize("broken_observer", [False, True])
@pytest.mark.parametrize("post_fails", [False, True])
def test_planner_observer_does_not_change_transport_outcome(
    monkeypatch, broken_observer, post_fails,
):
    calls = []
    original_error = httpx.ConnectError("local failure")
    class Client:
        def __init__(self, *, timeout):
            pass
        def post(self, endpoint, **kwargs):
            calls.append(kwargs)
            if post_fails:
                raise original_error
            return SimpleNamespace(status_code=200, text=json.dumps({
                "choices": [{"message": {"content": '{"intents":[]}'}}],
            }))
    def broken(record):
        raise RuntimeError("observer failure")
    monkeypatch.setattr(
        "ai_banking_copilot.planning.llm.importlib.import_module",
        lambda name: SimpleNamespace(Client=Client),
    )
    model = LlmIntentModel(api_key="local-fake", observer=broken if broken_observer else None)
    if post_fails:
        with pytest.raises(LlmUnavailableError) as caught:
            model.propose("查余额", {})
        assert caught.value.__cause__ is original_error
    else:
        assert model.propose("查余额", {}).intents == ()
    assert len(calls) == 1


def test_injected_transport_is_used_without_network() -> None:
    requests: list[str] = []

    def _transport(request: str) -> str:
        requests.append(request)
        return json.dumps(
            {
                "intents": [{
                    "action": "transfer.create",
                    "confidence": 0.8,
                    "evidence": "转给李娜",
                    "slots": {"span": "转给李娜 500 元"},
                }]
            }
        )

    proposal = LlmIntentModel(transport=_transport).propose("转给李娜 500 元", {})
    assert [intent.action for intent in proposal.intents] == ["transfer.create"]
    assert proposal.intents[0].confidence == 0.8
    assert len(requests) == 1


def test_strict_planner_accepts_constrained_semantic_frames() -> None:
    def transport(request: str) -> str:
        return json.dumps({"frames": [{
            "domain": "transfer", "operation": "create", "mode": "write",
            "evidence": "pay Li Na 500", "confidence": 0.9,
            "roles": {"amount": {"value": 50000, "role": "transfer_amount", "evidence": "500"}},
        }]})

    proposal = LlmIntentModel(transport=transport).propose("pay Li Na 500", {})

    assert proposal.channel == "llm-strict"
    assert proposal.frames[0].roles["amount"].role == "transfer_amount"
    assert proposal.intents[0].action == "transfer.create"


def test_strict_planner_rejects_frame_with_object_identifier() -> None:
    def transport(request: str) -> str:
        return json.dumps({"frames": [{
            "domain": "transfer", "operation": "create", "mode": "write",
            "evidence": "pay Li Na 500", "confidence": 0.9,
            "roles": {"payee_id": {"value": "p_lina", "role": "payee_hint", "evidence": "Li Na"}},
        }]})

    with pytest.raises(LlmUnavailableError, match="forbidden role"):
        LlmIntentModel(transport=transport).propose("pay Li Na 500", {})


@pytest.mark.parametrize("slots", [None, {}, {"span": 7}, {"span": "转给王五 500 元"}])
def test_missing_or_unanchored_llm_span_is_rejected(slots) -> None:
    def _transport(request: str) -> str:
        intent = {"action": "transfer.create", "confidence": 0.9}
        if slots is not None:
            intent["slots"] = slots
        return json.dumps({"intents": [intent]})

    with pytest.raises(LlmUnavailableError, match="span"):
        LlmIntentModel(transport=_transport).propose("转给李娜 500 元", {})


def test_invalid_llm_span_uses_only_the_deterministic_template_fallback() -> None:
    model = FallbackIntentModel(
        LlmIntentModel(
            transport=lambda request: json.dumps({
                "intents": [{"action": "transfer.create", "confidence": 0.9}]
            })
        )
    )

    proposal = model.propose("转给李娜 500 元", {})

    assert proposal.channel == "template_fallback"
    assert proposal.intents[0].slots["span"] == "转给李娜 500 元"
    assert model.degraded is True


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
    assert '"frames":[{"domain":"transfer"' in prompt
    assert "transfer / create / write -> transfer.create" in prompt
    assert "life_event / birthday_plan / schedule -> merchant.order" in prompt
    assert "frame" in prompt
    assert "budget_limit" in prompt and "order_amount" in prompt
    assert "不得输出工具名" in prompt and "多个可执行帧不得重叠" in prompt
    assert messages[1]["role"] == "user"
    assert f"【用户原话】\n{utterance}\n" in messages[1]["content"]


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


def test_query_instructions_accept_questions_without_amount_date_or_request_verb() -> None:
    requests: list[dict[str, object]] = []

    def transport(request: str) -> str:
        requests.append(json.loads(request))
        return json.dumps({"intents": []})

    utterance = "我花了多少钱"
    LlmIntentModel(transport=transport).propose(utterance, {})
    messages = requests[0]["messages"]
    prompt = messages[0]["content"]
    assert "budget_limit" in prompt and "investment_amount" in prompt
    assert "不得输出工具名" in prompt
    assert "缺少金额时不得猜测" in prompt
    assert messages[1]["role"] == "user"
    assert f"【用户原话】\n{utterance}\n" in messages[1]["content"]
