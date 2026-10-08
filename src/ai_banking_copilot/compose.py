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
import secrets
from collections.abc import Mapping
from pathlib import Path

from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.merchant_store import init_merchant_schema, seed_user_profiles
from ai_banking_copilot.gateway.risk_store import (
    StoreRiskAssessmentSource,
    init_risk_schema,
    seed_risk_assessment,
)
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.planning.llm import (
    DEFAULT_LLM_ENDPOINT,
    DEFAULT_LLM_MODEL,
    FallbackIntentModel,
    IntentModel,
    LlmIntentModel,
    TemplateIntentModel,
)
from ai_banking_copilot.policy.audit import AuditLog, ChainBrokenError
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.breaker_store import StoreBreakerStateStore, init_breaker_schema
from ai_banking_copilot.policy.pep import Pep
from ai_banking_copilot.policy.reversal import ReversalService
from ai_banking_copilot.scenarios.registry import QUERY_HANDLERS, WRITE_HANDLERS
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.web.app import AppDeps

DISABLE_LLM_ENV = "AIB_DISABLE_LLM"
"""置为真值时强制**只用模板通道**（现场演示保稳的可选开关）。"""

_DISABLED_VALUES = frozenset({"1", "true", "yes", "on"})

_KEY_ENV_NAMES = ("AI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY")
"""凭据环境变量（按序取首个非空）：通用 `AI_*` 优先，兼容本仓库历史 `DASHSCOPE_API_KEY`。"""


def build_intent_model(env: Mapping[str, str] | None = None) -> IntentModel:
    """装配意图通道：默认"真模型优先 + 失败降级模板"；`AIB_DISABLE_LLM` 真值时只用模板。

    端点与模型可经 `AI_BASE_URL`（或 `OPENAI_API_BASE_URL`）/ `AI_MODEL` 覆盖，
    未提供时沿用 `LlmIntentModel` 的默认值。
    """
    source = os.environ if env is None else env
    if source.get(DISABLE_LLM_ENV, "").strip().lower() in _DISABLED_VALUES:
        return TemplateIntentModel()
    api_key = ""
    for name in _KEY_ENV_NAMES:
        candidate = source.get(name, "").strip()
        if candidate:
            api_key = candidate
            break
    endpoint = (
        source.get("AI_BASE_URL", "").strip()
        or source.get("OPENAI_API_BASE_URL", "").strip()
        or DEFAULT_LLM_ENDPOINT
    )
    model = source.get("AI_MODEL", "").strip() or DEFAULT_LLM_MODEL
    return FallbackIntentModel(LlmIntentModel(api_key=api_key, model=model, endpoint=endpoint))


AUDIT_KEY_ENV = "AIB_AUDIT_KEY"
"""审计链 HMAC 密钥环境变量（优先级最高；生产环境应从密钥管理服务注入）。"""


def _audit_key(audit_path: Path, source: Mapping[str, str]) -> str:
    """取审计链密钥：环境变量优先；否则用同目录 `audit.jsonl.key`（不存在则生成随机密钥）。

    **局限（如实）**：密钥文件与日志同目录，具备该目录读写权限者可同时改写两者。
    详见 `policy/audit.py` 的模块说明。
    """
    from_env = source.get(AUDIT_KEY_ENV, "").strip()
    if from_env:
        return from_env
    key_file = Path(f"{audit_path}.key")
    if key_file.is_file():
        return key_file.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    key_file.write_text(key, encoding="utf-8")
    return key


def build_audit_log(db_path: Path, env: Mapping[str, str] | None = None) -> AuditLog:
    """装配审计链并**在启动时**校验历史全链；校验失败即拒绝启动（fail-closed）。"""
    source = os.environ if env is None else env
    audit_path = db_path.parent / "audit.jsonl"
    audit = AuditLog(audit_path, key=_audit_key(audit_path, source))
    if not audit.verify_chain():
        raise ChainBrokenError(
            f"审计链校验失败：{audit_path}（拒绝以被篡改或密钥不匹配的审计链启动）"
        )
    return audit


BREAKER_STATE_ENV = "AIB_BREAKER_STATE"
"""置为 `memory`（或 `0` / `false` / `off`）时熔断状态**只留进程内**；**默认落盘**（裁决 #27）。

落盘的**理由（完整性）**：内存态下「重启进程即把熔断复位」，等于给安全机制留了一条绕过路径。
落盘的**代价**（已由解锁出口抵消）：`OPEN` 会跨重启保留——这正是「只加严不降级」的正确语义，
而运维可用 `POST /api/operator/breaker-unlock`（需 `AIB_OPERATOR_TOKEN`）显式、留痕地恢复。
"""

_MEMORY_VALUES = frozenset({"memory", "0", "false", "no", "off", "none"})

OPERATOR_TOKEN_ENV = "AIB_OPERATOR_TOKEN"
"""运维口令（裁决 #27）：未配置 / 空白 ⇒ 解锁端点一律 403（fail-closed）。"""


def build_breaker(
    db_path: Path, env: Mapping[str, str] | None = None
) -> tuple[CircuitBreaker, Store | None]:
    """装配熔断器：**默认落盘**（跨重启 / 跨 worker）；`AIB_BREAKER_STATE=memory` 退回进程内。

    返回 `(breaker, store)`，`store` 非空表示持有写连接（由调用方持有到进程结束）。
    """
    source = os.environ if env is None else env
    if source.get(BREAKER_STATE_ENV, "").strip().lower() in _MEMORY_VALUES:
        return CircuitBreaker(), None
    store = Store(db_path, StoreMode.READ_WRITE)
    return CircuitBreaker(store=StoreBreakerStateStore(store)), store


def build_operator_token(env: Mapping[str, str] | None = None) -> str | None:
    """运维口令：**空 / 纯空白视为未配置**（`None` ⇒ 解锁端点 fail-closed 403）。"""
    source = os.environ if env is None else env
    return source.get(OPERATOR_TOKEN_ENV, "").strip() or None


def build_deps(db_path: Path, env: Mapping[str, str] | None = None) -> AppDeps:
    """装配默认依赖：模拟核心银行 + 只读事实网关 + 全量策略/工具依赖。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    writer = Store(db_path)
    init_schema(writer)
    seed_database(writer)
    init_risk_schema(writer)
    seed_risk_assessment(writer)
    init_merchant_schema(writer)
    seed_user_profiles(writer)
    init_breaker_schema(writer)
    writer.close()

    read_store = Store(db_path, StoreMode.READ_ONLY)
    gateway = FactGateway(read_store)
    registry = build_default_registry()
    breaker, _breaker_store = build_breaker(db_path, env)
    audit = build_audit_log(db_path, env)
    core_bank = CoreBank(Store(db_path, StoreMode.READ_WRITE))
    return AppDeps(
        intent_model=build_intent_model(env),
        registry=registry,
        gateway=gateway,
        pep=Pep(registry, gateway, core_bank, audit, breaker, WRITE_HANDLERS, QUERY_HANDLERS),
        breaker=breaker,
        audit=audit,
        user_id=DEMO_USER_ID,
        risk=StoreRiskAssessmentSource(read_store),
        reversal=ReversalService(core_bank),
        operator_token=build_operator_token(env),
    )
