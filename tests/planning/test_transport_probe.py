import json

import ai_banking_copilot.planning.transport_probe as transport_probe
from ai_banking_copilot.planning.transport_probe import (
    ProbeClass,
    probe_chat_completion,
)


def test_default_client_is_closed_after_probe(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class Response:
        status_code = 200
        text = json.dumps({"choices": [{"message": {"content": "ok"}}]})

    class Client:
        closed = False

        def post(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

        def close(self) -> None:
            self.closed = True

    client = Client()
    monkeypatch.setattr(transport_probe, "_default_client_factory", lambda *, timeout: client)

    result = probe_chat_completion(
        channel="planner",
        endpoint="https://example.invalid/v1/chat/completions",
        api_key="secret",
        payload={"model": "qwen-plus", "messages": []},
        timeout_s=20.0,
    )

    assert result.classification is ProbeClass.OK
    assert client.closed is True


def test_default_client_is_closed_after_transport_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class Client:
        closed = False

        def post(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("transport failed")

        def close(self) -> None:
            self.closed = True

    client = Client()
    monkeypatch.setattr(transport_probe, "_default_client_factory", lambda *, timeout: client)

    result = probe_chat_completion(
        channel="planner",
        endpoint="https://example.invalid/v1/chat/completions",
        api_key="secret",
        payload={"model": "qwen-plus", "messages": []},
        timeout_s=20.0,
    )

    assert result.classification is ProbeClass.TRANSPORT_ERROR
    assert len(result.attempts) == 2
    assert client.closed is True


def test_injected_client_stays_open_after_probe() -> None:
    class Response:
        status_code = 200
        text = json.dumps({"choices": [{"message": {"content": "ok"}}]})

    class Client:
        closed = False

        def post(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

        def close(self) -> None:
            self.closed = True

    client = Client()
    result = probe_chat_completion(
        channel="planner",
        endpoint="https://example.invalid/v1/chat/completions",
        api_key="secret",
        payload={"model": "qwen-plus", "messages": []},
        timeout_s=20.0,
        client_factory=lambda *, timeout: client,  # type: ignore[no-untyped-def]
    )

    assert result.classification is ProbeClass.OK
    assert client.closed is False


def test_missing_key_does_not_call_client() -> None:
    called = False

    def factory(*, timeout: float):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        raise AssertionError("client must not be constructed")

    result = probe_chat_completion(
        channel="planner",
        endpoint="https://example.invalid/v1/chat/completions",
        api_key="",
        payload={"model": "qwen-plus", "messages": []},
        timeout_s=20.0,
        client_factory=factory,
    )
    assert result.classification is ProbeClass.CONFIG_MISSING
    assert result.attempts == ()
    assert called is False


def test_success_extracts_model_content_without_publicly_exposing_it() -> None:
    class Response:
        status_code = 200
        text = json.dumps(
            {"choices": [{"message": {"content": '{"ok":true}'}}], "usage": {"total_tokens": 3}}
        )

    class Client:
        def post(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    result = probe_chat_completion(
        channel="planner",
        endpoint="https://example.invalid/v1/chat/completions",
        api_key="secret",
        payload={"model": "qwen-plus", "messages": []},
        timeout_s=20.0,
        client_factory=lambda *, timeout: Client(),  # type: ignore[no-untyped-def]
    )
    assert result.classification is ProbeClass.OK
    assert result.model_text == '{"ok":true}'
    assert result.raw_response == Response.text
    assert "raw_response" not in repr(result)
    assert "model_text" not in repr(result)
    assert "secret" not in json.dumps(result.public_dict(), ensure_ascii=False)
    assert result.raw_response not in json.dumps(result.public_dict())


def test_timeout_retries_once_then_reports_timeout() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise TimeoutError("timed out")

    client = Client()
    result = probe_chat_completion(
        channel="reviewer",
        endpoint="https://example.invalid/v1/chat/completions",
        api_key="secret",
        payload={"model": "qwen-plus", "messages": []},
        timeout_s=20.0,
        client_factory=lambda *, timeout: client,  # type: ignore[no-untyped-def]
    )
    assert result.classification is ProbeClass.TIMEOUT
    assert len(result.attempts) == 2
    assert client.calls == 2


def test_malformed_success_response_is_protocol_error() -> None:
    class Response:
        status_code = 200
        text = "{}"

    class Client:
        def post(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    result = probe_chat_completion(
        channel="planner",
        endpoint="https://example.invalid/v1/chat/completions",
        api_key="secret",
        payload={"model": "qwen-plus", "messages": []},
        timeout_s=20.0,
        client_factory=lambda *, timeout: Client(),  # type: ignore[no-untyped-def]
    )
    assert result.classification is ProbeClass.PROTOCOL_ERROR
    assert result.model_text is None


def test_http_503_then_success_retries_once() -> None:
    class Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.text = json.dumps({"choices": [{"message": {"content": "ok"}}]})

    class Client:
        calls = 0

        def post(self, *_args: object, **_kwargs: object) -> Response:
            self.calls += 1
            return Response(503 if self.calls == 1 else 200)

    client = Client()
    result = probe_chat_completion(
        channel="planner",
        endpoint="https://example.invalid",
        api_key="secret",
        payload={},
        timeout_s=20.0,
        client_factory=lambda *, timeout: client,
    )
    assert client.calls == 2
    assert result.classification is ProbeClass.OK
    assert [item.classification for item in result.attempts] == [
        ProbeClass.HTTP_ERROR,
        ProbeClass.OK,
    ]
    assert [item.http_status for item in result.attempts] == [503, 200]


def test_bad_envelope_retries_but_single_attempt_budget_stops() -> None:
    class Response:
        status_code = 200
        text = "{}"

    class Client:
        calls = 0

        def post(self, *_args: object, **_kwargs: object) -> Response:
            self.calls += 1
            return Response()

    client = Client()
    result = probe_chat_completion(
        channel="planner",
        endpoint="https://example.invalid",
        api_key="secret",
        payload={},
        timeout_s=20.0,
        client_factory=lambda *, timeout: client,
    )
    assert client.calls == 2
    assert [item.classification for item in result.attempts] == [ProbeClass.PROTOCOL_ERROR] * 2
    client.calls = 0
    result = probe_chat_completion(
        channel="planner",
        endpoint="https://example.invalid",
        api_key="secret",
        payload={},
        timeout_s=20.0,
        client_factory=lambda *, timeout: client,
        max_attempts=1,
    )
    assert client.calls == 1
    assert len(result.attempts) == 1
