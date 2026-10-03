"""组合根：按环境装配依赖（意图通道真模型优先 + 失败降级；写面由本层承担）。

放**包根**而非 `web/`：`web/` 层源码不得直接触碰 `CoreBank`（H2 守卫），故写面装配
（`CoreBank` + `Pep`）与意图通道装配都由本组合根承担；`web/` 只消费注入好的 `AppDeps`。

LLM 纪律（H9 / spec §4.4）：
- Key 只走环境变量，不入库 / 不入日志 / 不入提交物；
- `LlmIntentModel` **懒加载**（构造期不碰网络）；缺 Key / 缺 httpx / 调用失败一律
  抛 `LlmUnavailableError` → `FallbackIntentModel` 回落模板通道（能力子集，非等价）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.risk_store import (
    StoreRiskAssessmentSource,
    init_risk_schema,
    seed_risk_assessment,
)
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.planning.llm import (
    FallbackIntentModel,
    IntentModel,
    LlmIntentModel,
    TemplateIntentModel,
)
from ai_banking_copilot.policy.audit import AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.pep import Pep
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.web.app import AppDeps

DISABLE_LLM_ENV = "AIB_DISABLE_LLM"
"""置为真值时强制**只用模板通道**（现场演示保稳的可选开关）。"""

_DISABLED_VALUES = frozenset({"1", "true", "yes", "on"})


def build_intent_model(env: Mapping[str, str] | None = None) -> IntentModel:
    """装配意图通道：默认"真模型优先 + 失败降级模板"；`AIB_DISABLE_LLM` 真值时只用模板。"""
    source = os.environ if env is None else env
    if source.get(DISABLE_LLM_ENV, "").strip().lower() in _DISABLED_VALUES:
        return TemplateIntentModel()
    api_key = source.get("DASHSCOPE_API_KEY", "")
    return FallbackIntentModel(LlmIntentModel(api_key=api_key))


def build_deps(db_path: Path, env: Mapping[str, str] | None = None) -> AppDeps:
    """装配默认依赖：模拟核心银行 + 只读事实网关 + 全量策略/工具依赖。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    writer = Store(db_path)
    init_schema(writer)
    seed_database(writer)
    init_risk_schema(writer)
    seed_risk_assessment(writer)
    writer.close()

    read_store = Store(db_path, StoreMode.READ_ONLY)
    gateway = FactGateway(read_store)
    registry = build_default_registry()
    breaker = CircuitBreaker()
    audit = AuditLog(db_path.parent / "audit.jsonl")
    return AppDeps(
        intent_model=build_intent_model(env),
        registry=registry,
        gateway=gateway,
        pep=Pep(registry, gateway, CoreBank(Store(db_path, StoreMode.READ_WRITE)), audit, breaker),
        breaker=breaker,
        audit=audit,
        user_id=DEMO_USER_ID,
        risk=StoreRiskAssessmentSource(read_store),
    )
