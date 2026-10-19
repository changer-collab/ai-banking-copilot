"""可测试的聊天完成接口传输探针。

探针只在内存中保留模型正文，公开结果仅包含分类、尝试摘要和数值用量。
"""

from __future__ import annotations

import importlib
import json
import time
from collections.abc import Callable, Mapping
from contextlib import closing, nullcontext
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Protocol, cast


class ProbeClass(StrEnum):
    CONFIG_MISSING = "config_missing"
    TRANSPORT_ERROR = "transport_error"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    PROTOCOL_ERROR = "protocol_error"
    OK = "ok"


@dataclass(frozen=True)
class ProbeAttempt:
    number: int
    classification: ProbeClass | str
    latency_ms: float
    http_status: int | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    channel: str
    classification: ProbeClass
    attempts: tuple[ProbeAttempt, ...]
    model_text: str | None = field(default=None, repr=False)
    usage: Mapping[str, int | float] | None = None
    raw_response: str | None = field(default=None, repr=False)

    def public_dict(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "classification": self.classification.value,
            "attempts": [asdict(item) for item in self.attempts],
            "usage": dict(self.usage or {}),
        }


class _Response(Protocol):
    status_code: int
    text: str


class _Client(Protocol):
    def post(
        self,
        url: str,
        *,
        json: Mapping[str, object],
        headers: Mapping[str, str],
        timeout: float,
    ) -> _Response: ...


class _CloseableClient(_Client, Protocol):
    def close(self) -> None: ...


class _TimeoutError(Exception):
    """Marker used to classify timeout exceptions without importing httpx eagerly."""


def _is_timeout(error: BaseException) -> bool:
    if isinstance(error, TimeoutError):
        return True
    try:
        httpx = importlib.import_module("httpx")
    except ImportError:
        return False
    timeout_type = getattr(httpx, "TimeoutException", _TimeoutError)
    return isinstance(error, timeout_type)


def _default_client_factory(*, timeout: float) -> object:
    try:
        module = importlib.import_module("httpx")
    except ImportError as error:
        raise RuntimeError("未安装可选依赖 httpx") from error
    return module.Client(timeout=timeout)


def _numeric_usage(value: object) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, (int, float)) and not isinstance(item, bool)
    }


def _extract_response(response: _Response) -> tuple[str, dict[str, int | float]]:
    try:
        envelope = json.loads(response.text)
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError("响应不是合法 JSON") from error
    if not isinstance(envelope, dict):
        raise ValueError("响应必须是 JSON 对象")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("响应缺少 choices[0]")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("响应缺少 choices[0].message.content")
    return message["content"], _numeric_usage(envelope.get("usage"))


def probe_chat_completion(
    *,
    channel: str,
    endpoint: str,
    api_key: str,
    payload: Mapping[str, object],
    timeout_s: float,
    client_factory: Callable[..., object] | None = None,
    max_attempts: int = 2,
) -> ProbeResult:
    """调用聊天完成接口；单独使用时失败最多重试一次。"""
    if max_attempts not in (1, 2):
        raise ValueError("max_attempts must be 1 or 2")
    if not api_key:
        return ProbeResult(channel=channel, classification=ProbeClass.CONFIG_MISSING, attempts=())

    owns_client = client_factory is None
    factory = client_factory or _default_client_factory
    try:
        client = cast(_Client, factory(timeout=timeout_s))
    except Exception as error:
        attempt = ProbeAttempt(
            number=1,
            classification=ProbeClass.TRANSPORT_ERROR,
            latency_ms=0.0,
            error_type=type(error).__name__,
        )
        return ProbeResult(
            channel=channel,
            classification=ProbeClass.TRANSPORT_ERROR,
            attempts=(attempt,),
        )

    client_scope = closing(cast(_CloseableClient, client)) if owns_client else nullcontext(client)
    with client_scope:
        attempts: list[ProbeAttempt] = []
        for number in range(1, max_attempts + 1):
            started = time.perf_counter()
            try:
                response = client.post(
                    endpoint,
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=timeout_s,
                )
            except Exception as error:
                classification = (
                    ProbeClass.TIMEOUT if _is_timeout(error) else ProbeClass.TRANSPORT_ERROR
                )
                attempts.append(
                    ProbeAttempt(
                        number=number,
                        classification=classification,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        error_type=type(error).__name__,
                    )
                )
                if number < max_attempts:
                    continue
                return ProbeResult(
                    channel=channel,
                    classification=classification,
                    attempts=tuple(attempts),
                )

            latency_ms = (time.perf_counter() - started) * 1000
            if not 200 <= response.status_code < 300:
                attempt = ProbeAttempt(
                    number=number,
                    classification=ProbeClass.HTTP_ERROR,
                    latency_ms=latency_ms,
                    http_status=response.status_code,
                )
                attempts.append(attempt)
                if number < max_attempts:
                    continue
                return ProbeResult(channel, ProbeClass.HTTP_ERROR, tuple(attempts))
            try:
                model_text, usage = _extract_response(response)
            except ValueError as error:
                attempt = ProbeAttempt(
                    number=number,
                    classification=ProbeClass.PROTOCOL_ERROR,
                    latency_ms=latency_ms,
                    http_status=response.status_code,
                    error_type=type(error).__name__,
                )
                attempts.append(attempt)
                if number < max_attempts:
                    continue
                return ProbeResult(channel, ProbeClass.PROTOCOL_ERROR, tuple(attempts))
            attempt = ProbeAttempt(
                number=number,
                classification=ProbeClass.OK,
                latency_ms=latency_ms,
                http_status=response.status_code,
            )
            return ProbeResult(
                channel=channel,
                classification=ProbeClass.OK,
                attempts=tuple((*attempts, attempt)),
                model_text=model_text,
                usage=usage,
                raw_response=response.text,
            )

    raise AssertionError("probe loop must return")
