"""组合根装配：意图通道真模型优先、失败/禁用降级模板（全程不联网）。"""

from __future__ import annotations

from pathlib import Path

from ai_banking_copilot.compose import DISABLE_LLM_ENV, build_deps, build_intent_model
from ai_banking_copilot.planning.llm import FallbackIntentModel, TemplateIntentModel
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


def test_build_deps_wires_fallback_intent_model(tmp_path: Path) -> None:
    deps = build_deps(tmp_path / "copilot.db", env={})
    assert isinstance(deps, AppDeps)
    assert isinstance(deps.intent_model, FallbackIntentModel)
