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
)
from ai_banking_copilot.planning.llm import FallbackIntentModel, TemplateIntentModel
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
