import json
from types import SimpleNamespace

import httpx
import pytest

from ai_banking_copilot.planning.llm import (
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


def test_query_instructions_accept_questions_without_amount_date_or_request_verb() -> None:
    requests: list[dict[str, object]] = []

    def transport(request: str) -> str:
        requests.append(json.loads(request))
        return json.dumps({"intents": []})

    utterance = "我花了多少钱"
    LlmIntentModel(transport=transport).propose(utterance, {})
    messages = requests[0]["messages"]
    prompt = messages[0]["content"]
    assert "查询不要求出现请求词、金额或日期" in prompt
    assert "缺少的控制槽交由服务端补全或澄清" in prompt
    assert "用户明确给出的对应金额或日期必须保留" in prompt
    assert messages[1] == {"role": "user", "content": utterance}
