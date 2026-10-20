"""组合根装配：意图通道真模型优先、失败/禁用降级模板（全程不联网）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_banking_copilot.compose import (
    AUDIT_KEY_ENV,
    DISABLE_LLM_ENV,
    build_audit_log,
    build_deps,
    build_intent_model,
    build_reviewer,
    reviewer_required,
)
from ai_banking_copilot.planning.llm import (
    DEFAULT_LLM_ENDPOINT,
    DEFAULT_LLM_MODEL,
    FallbackIntentModel,
    TemplateIntentModel,
)
from ai_banking_copilot.planning.reviewer import BypassPlanReviewer, Reviewer
from ai_banking_copilot.policy.audit import KIND_EXECUTION, AuditEvent, ChainBrokenError
from ai_banking_copilot.web.app import AppDeps


def test_disabled_env_is_template_only() -> None:
    model = build_intent_model({DISABLE_LLM_ENV: "1"})
    assert isinstance(model, TemplateIntentModel)


def test_missing_key_degrades_to_template_without_network() -> None:
    model = build_intent_model({})
    assert isinstance(model, FallbackIntentModel)
    proposal = model.propose("转给李娜 500 元", {})
    assert [item.action for item in proposal.intents] == ["transfer.create"]
    assert model.degraded is True


def test_key_present_keeps_llm_primary_lazy() -> None:
    model = build_intent_model({"DASHSCOPE_API_KEY": "sk-test"})
    assert isinstance(model, FallbackIntentModel)
    assert model.degraded is False


@pytest.mark.parametrize(
    ("env", "endpoint", "model", "api_key"),
    [
        (
            {
                "AI_BASE_URL": " https://primary.example/chat/completions ",
                "OPENAI_API_BASE_URL": "https://secondary.example/chat/completions",
                "AI_MODEL": " injected-model ",
                "AI_API_KEY": " primary-test-credential ",
                "DEEPSEEK_API_KEY": "secondary-test-credential",
                "DASHSCOPE_API_KEY": "legacy-test-credential",
            },
            "https://primary.example/chat/completions", "injected-model", "primary-test-credential",
        ),
        (
            {
                "AI_BASE_URL": " ",
                "OPENAI_API_BASE_URL": " https://compatible.example/chat/completions ",
                "AI_MODEL": " compatible-model ",
                "AI_API_KEY": " ",
                "DEEPSEEK_API_KEY": " compatible-test-credential ",
                "DASHSCOPE_API_KEY": "legacy-test-credential",
            },
            "https://compatible.example/chat/completions",
            "compatible-model", "compatible-test-credential",
        ),
        (
            {"DASHSCOPE_API_KEY": " legacy-test-credential "},
            DEFAULT_LLM_ENDPOINT, DEFAULT_LLM_MODEL, "legacy-test-credential",
        ),
        ({}, DEFAULT_LLM_ENDPOINT, DEFAULT_LLM_MODEL, ""),
    ],
)
def test_reviewer_receives_injected_provider_configuration(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str], endpoint: str, model: str, api_key: str,
) -> None:
    captured: dict[str, object] = {}

    def factory(mode: str, **kwargs: object) -> Reviewer:
        captured.update({"mode": mode, **kwargs})
        return BypassPlanReviewer()

    monkeypatch.setattr("ai_banking_copilot.compose.create_reviewer", factory)
    build_reviewer({**env, "AIB_REVIEW_CHANNEL": "llm-strict"})
    assert captured == {
        "mode": "llm-strict", "endpoint": endpoint, "model": model, "api_key": api_key,
    }


def test_disabled_llm_forces_reviewer_bypass_even_when_strict_requested() -> None:
    reviewer = build_reviewer({DISABLE_LLM_ENV: "1", "AIB_REVIEW_CHANNEL": "llm-strict"})
    assert isinstance(reviewer, BypassPlanReviewer)


def test_default_reviewer_remains_bypass() -> None:
    assert isinstance(build_reviewer({}), BypassPlanReviewer)


def test_strict_channel_always_requires_review_unless_llm_is_disabled() -> None:
    assert reviewer_required({"AIB_REVIEW_CHANNEL": "llm-strict"}) is True
    assert reviewer_required({"AIB_REVIEW_CHANNEL": "llm-strict", "AIB_REVIEW_REQUIRED": "0"})
    assert reviewer_required({"AIB_REVIEW_CHANNEL": "llm-strict", DISABLE_LLM_ENV: "1"}) is False


def test_composed_roles_share_config_budget(tmp_path: Path) -> None:
    deps = build_deps(tmp_path / "shared.db", env={
        "AIB_REVIEW_CHANNEL": "llm-strict", "AI_API_KEY": "test-key",
        "AI_PLANNER_TIMEOUT_S": "12", "AI_REVIEWER_TIMEOUT_S": "90",
    })
    assert isinstance(deps.intent_model, FallbackIntentModel)
    assert deps.reviewer is not None
    assert deps.intent_model._primary._limiter is deps.reviewer._limiter
    assert deps.intent_model._primary._timeout_s == 12
    assert deps.intent_model._primary._prompt_version == "planner-v1"
    assert deps.reviewer._timeout_s == 90
    assert deps.reviewer_required is True


def test_build_deps_wires_fallback_intent_model(tmp_path: Path) -> None:
    deps = build_deps(tmp_path / "copilot.db", env={})
    assert isinstance(deps, AppDeps)
    assert isinstance(deps.intent_model, FallbackIntentModel)


def test_build_audit_log_verifies_the_chain_at_startup(tmp_path: Path) -> None:
    """启动时校验历史全链：被篡改即拒绝启动（fail-closed）。"""
    db_path = tmp_path / "copilot.db"
    audit = build_audit_log(db_path, env={})
    assert audit.keyed is True  # 未设环境变量时回退到同目录密钥文件
    audit.append(AuditEvent(event_id="e1", kind=KIND_EXECUTION, payload={}, prev_hash=""))

    log_path = tmp_path / "audit.jsonl"
    log_path.write_text(
        log_path.read_text(encoding="utf-8").replace('"e1"', '"e2"'), encoding="utf-8"
    )
    with pytest.raises(ChainBrokenError, match="审计链校验失败"):
        build_audit_log(db_path, env={})


def test_build_audit_log_prefers_the_env_key(tmp_path: Path) -> None:
    audit = build_audit_log(tmp_path / "copilot.db", env={AUDIT_KEY_ENV: "k-from-env"})
    assert audit.keyed is True
    assert not (tmp_path / "audit.jsonl.key").exists()
