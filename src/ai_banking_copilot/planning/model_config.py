"""统一的模型配置与非阻塞调用预算。"""

from __future__ import annotations

import math
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DEFAULT_MODEL = "qwen-plus"
DEFAULT_PLANNER_TIMEOUT_S = 20.0
DEFAULT_REVIEWER_TIMEOUT_S = 180.0
DEFAULT_LLM_ENDPOINT = DEFAULT_ENDPOINT
DEFAULT_LLM_MODEL = DEFAULT_MODEL


class _Lease:
    def __init__(self, limiter: NonBlockingLimiter) -> None:
        self._limiter = limiter
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._limiter._release()

    def __enter__(self) -> _Lease:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class NonBlockingLimiter:
    """非阻塞并发闸门；额度耗尽时返回 ``None``，不等待、不重试。"""

    def __init__(self, capacity: int = 1) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._semaphore = threading.BoundedSemaphore(capacity)

    def try_acquire(self) -> _Lease | None:
        if not self._semaphore.acquire(blocking=False):
            return None
        return _Lease(self)

    def _release(self) -> None:
        self._semaphore.release()


def _first(source: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = str(source.get(name, "")).strip()
        if value:
            return value
    return ""


def _positive(source: Mapping[str, str], name: str, default: float) -> float:
    raw = str(source.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须为正数") from error
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须为正数")
    return value


@dataclass(frozen=True, repr=False)
class ModelConfig:
    """模型配置快照；密钥仅供传输层使用，不出现在 repr 或公开元数据。"""

    api_key: str = field(default="", repr=False)
    api_key_env: str | None = None
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    planner_model: str = DEFAULT_MODEL
    reviewer_model: str = DEFAULT_MODEL
    planner_timeout_s: float = DEFAULT_PLANNER_TIMEOUT_S
    reviewer_timeout_s: float = DEFAULT_REVIEWER_TIMEOUT_S
    max_attempts: int = 1
    max_concurrency: int = 1
    disable_llm: bool = False
    limiter: NonBlockingLimiter = field(
        default_factory=NonBlockingLimiter, repr=False, compare=False,
    )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ModelConfig:
        source = os.environ if env is None else env
        key_name = next(
            (name for name in ("AI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY")
             if str(source.get(name, "")).strip()),
            None,
        )
        key = str(source.get(key_name, "")).strip() if key_name else ""
        endpoint = _first(source, "AI_BASE_URL", "OPENAI_API_BASE_URL") or DEFAULT_ENDPOINT
        model = _first(source, "AI_MODEL", "OPENAI_MODEL") or DEFAULT_MODEL
        planner_model = _first(source, "AI_PLANNER_MODEL", "PLANNER_MODEL") or model
        reviewer_model = _first(source, "AI_REVIEWER_MODEL", "REVIEWER_MODEL") or model
        common_timeout = _positive(source, "AI_TIMEOUT_S", DEFAULT_PLANNER_TIMEOUT_S)
        planner_timeout = _positive(
            source,
            "AI_PLANNER_TIMEOUT_S",
            _positive(source, "PLANNER_TIMEOUT_S", common_timeout),
        )
        reviewer_default = (
            DEFAULT_REVIEWER_TIMEOUT_S
            if not str(source.get("AI_TIMEOUT_S", "")).strip() else common_timeout
        )
        reviewer_timeout = _positive(
            source,
            "AI_REVIEWER_TIMEOUT_S",
            _positive(source, "REVIEWER_TIMEOUT_S", reviewer_default),
        )
        attempts_raw = str(source.get("AI_MAX_ATTEMPTS", "1")).strip() or "1"
        try:
            attempts = int(attempts_raw)
        except ValueError as error:
            raise ValueError("AI_MAX_ATTEMPTS 必须为 1") from error
        if attempts != 1:
            raise ValueError("AI_MAX_ATTEMPTS 必须为 1")
        concurrency_raw = str(source.get("AI_MAX_CONCURRENCY", "1")).strip() or "1"
        try:
            concurrency = int(concurrency_raw)
        except ValueError as error:
            raise ValueError("AI_MAX_CONCURRENCY 必须为正整数") from error
        if concurrency < 1:
            raise ValueError("AI_MAX_CONCURRENCY 必须为正整数")
        disabled = str(source.get("AIB_DISABLE_LLM", "")).strip().lower() in {
            "1", "true", "yes", "on"
        }
        return cls(
            api_key=key,
            api_key_env=key_name,
            endpoint=endpoint,
            model=model,
            planner_model=planner_model,
            reviewer_model=reviewer_model,
            planner_timeout_s=planner_timeout,
            reviewer_timeout_s=reviewer_timeout,
            max_attempts=1,
            max_concurrency=concurrency,
            disable_llm=disabled,
            limiter=NonBlockingLimiter(concurrency),
        )

    def public_metadata(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "api_key_env": self.api_key_env,
            "model": self.model,
            "planner_model": self.planner_model,
            "reviewer_model": self.reviewer_model,
            "planner_timeout_s": self.planner_timeout_s,
            "reviewer_timeout_s": self.reviewer_timeout_s,
            "max_attempts": self.max_attempts,
            "max_concurrency": self.max_concurrency,
            "llm_disabled": self.disable_llm,
        }

    def __repr__(self) -> str:
        fields = self.public_metadata()
        body = ", ".join(f"{key}={value!r}" for key, value in fields.items())
        return f"ModelConfig({body})"
