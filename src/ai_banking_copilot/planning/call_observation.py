"""只采集 HTTP 元数据与供应商用量，不传递正文或异常内容。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import perf_counter
from types import MappingProxyType


@dataclass(frozen=True, init=False)
class CallObservation:
    role: str
    http_status: int | None
    latency_ms: float
    failure_code: str | None
    usage: Mapping[str, int] | None

    def __init__(
        self,
        role: str,
        http_status: int | None,
        latency_ms: float,
        failure_code: str | None,
        usage: Mapping[str, int] | None,
        *,
        task_id: str | None = None,
        attempt_no: int | None = None,
        channel: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
    ) -> None:
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "http_status", http_status)
        object.__setattr__(self, "latency_ms", latency_ms)
        object.__setattr__(self, "failure_code", failure_code)
        object.__setattr__(
            self, "usage", MappingProxyType(dict(usage)) if usage is not None else None,
        )
        # 延迟保存可选元数据，保持旧位置参数和 vars(record) 的原有形状。
        for name, value in {
            "task_id": task_id,
            "attempt_no": attempt_no,
            "channel": channel,
            "model": model,
            "prompt_version": prompt_version,
        }.items():
            if value is not None:
                object.__setattr__(self, f"_{name}", value)

    @property
    def task_id(self) -> str | None:
        return getattr(self, "_task_id", None)

    @property
    def attempt_no(self) -> int | None:
        return getattr(self, "_attempt_no", None)

    @property
    def channel(self) -> str | None:
        return getattr(self, "_channel", None)

    @property
    def model(self) -> str | None:
        return getattr(self, "_model", None)

    @property
    def prompt_version(self) -> str | None:
        return getattr(self, "_prompt_version", None)

    def public_dict(self) -> dict[str, object]:
        """只导出固定元数据，不包含响应正文或异常原文。"""
        return {
            "role": self.role, "http_status": self.http_status,
            "latency_ms": self.latency_ms, "failure_code": self.failure_code,
            "usage": dict(self.usage) if self.usage is not None else None,
            "task_id": self.task_id, "attempt_no": self.attempt_no,
            "channel": self.channel, "model": self.model,
            "prompt_version": self.prompt_version,
        }


CallObserver = Callable[[CallObservation], None]


def provider_usage(response_text: str | None) -> Mapping[str, int] | None:
    if response_text is None:
        return None
    try:
        envelope = json.loads(response_text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("usage"), dict):
        return None
    raw = envelope["usage"]
    usage: dict[str, int] = {}
    for key in (
        "prompt_tokens", "completion_tokens", "total_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    ):
        value = raw.get(key)
        if type(value) is int and value >= 0:
            usage[key] = value
    for group, key in (
        ("prompt_tokens_details", "cached_tokens"),
        ("completion_tokens_details", "reasoning_tokens"),
    ):
        details = raw.get(group)
        if isinstance(details, dict):
            value = details.get(key)
            if type(value) is int and value >= 0:
                usage[f"{group}.{key}"] = value
    return usage or None


_provider_usage = provider_usage


def emit_http_observation(
    observer: CallObserver | None,
    *,
    role: str,
    started_at: float,
    http_status: int | None,
    response_text: str | None,
    error: BaseException | None,
    task_id: str | None = None,
    attempt_no: int | None = None,
    channel: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
) -> None:
    if observer is None:
        return
    failure_code = None
    if http_status is not None and http_status != 200:
        failure_code = "http_error"
    elif error is not None:
        # httpx 是可选依赖；按其已知异常基类识别，不加载客户端。
        is_timeout = isinstance(error, TimeoutError) or any(
            base.__module__ == "httpx" and base.__name__ == "TimeoutException"
            for base in type(error).__mro__
        )
        failure_code = "timeout" if is_timeout else "transport_error"
    try:
        observer(CallObservation(
            role=role, http_status=http_status,
            latency_ms=(perf_counter() - started_at) * 1000,
            failure_code=failure_code, usage=provider_usage(response_text),
            task_id=task_id, attempt_no=attempt_no, channel=channel,
            model=model, prompt_version=prompt_version,
        ))
    except Exception:
        # 观测失败不能改变产品结果，也不能覆盖原始 HTTP 异常。
        pass
