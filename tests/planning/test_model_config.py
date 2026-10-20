from dataclasses import FrozenInstanceError

import pytest

from ai_banking_copilot.planning.model_config import ModelConfig, NonBlockingLimiter


def test_model_config_preserves_alias_priority_and_hides_secret() -> None:
    config = ModelConfig.from_env(
        {
            "AI_API_KEY": " primary-secret ",
            "DEEPSEEK_API_KEY": "deep-secret",
            "DASHSCOPE_API_KEY": "dash-secret",
            "AI_BASE_URL": " https://primary.example/v1 ",
            "OPENAI_API_BASE_URL": "https://secondary.example/v1",
            "AI_MODEL": " primary-model ",
            "PLANNER_MODEL": "planner-model",
            "REVIEWER_MODEL": "reviewer-model",
            "AI_TIMEOUT_S": "9",
        }
    )
    assert config.api_key == "primary-secret"
    assert config.endpoint == "https://primary.example/v1"
    assert config.model == "primary-model"
    assert config.planner_model == "planner-model"
    assert config.reviewer_model == "reviewer-model"
    assert config.planner_timeout_s == 9
    assert config.reviewer_timeout_s == 9
    assert "primary-secret" not in repr(config)
    assert "primary-secret" not in str(config.public_metadata())
    with pytest.raises(FrozenInstanceError):
        config.model = "changed"  # type: ignore[misc]


def test_role_timeout_defaults_and_invalid_values() -> None:
    config = ModelConfig.from_env({"AI_PLANNER_TIMEOUT_S": "3", "AI_REVIEWER_TIMEOUT_S": "7"})
    assert config.planner_timeout_s == 3
    assert config.reviewer_timeout_s == 7
    with pytest.raises(ValueError, match="AI_TIMEOUT_S"):
        ModelConfig.from_env({"AI_TIMEOUT_S": "0"})


def test_nonblocking_limiter_fails_closed_when_full() -> None:
    limiter = NonBlockingLimiter(1)
    lease = limiter.try_acquire()
    assert lease is not None
    assert limiter.try_acquire() is None
    lease.release()
    lease.release()
    assert limiter.try_acquire() is not None
