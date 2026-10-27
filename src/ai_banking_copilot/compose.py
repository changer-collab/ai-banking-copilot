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
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.merchant_store import init_merchant_schema, seed_user_profiles
from ai_banking_copilot.gateway.receipt_store import StoreReceiptReader
from ai_banking_copilot.gateway.risk_store import (
    StoreRiskAssessmentSource,
    StoreRiskAssessmentWriter,
    init_risk_schema,
    seed_risk_assessment,
)
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database, seed_secondary_user
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.gateway.transfer_store import init_transfer_schema
from ai_banking_copilot.planning.analysis_codegen import AnalysisTask, generate_analysis_code
from ai_banking_copilot.planning.call_observation import CallObserver
from ai_banking_copilot.planning.llm import (
    FallbackIntentModel,
    IntentModel,
    LlmIntentModel,
    TemplateIntentModel,
)
from ai_banking_copilot.planning.model_config import DEFAULT_REVIEWER_TIMEOUT_S, ModelConfig
from ai_banking_copilot.planning.reviewer import (
    ReviewChannel,
    Reviewer,
    StrictPlanReviewer,
    create_reviewer,
)
from ai_banking_copilot.planning.task_observations import TaskObservations
from ai_banking_copilot.policy.audit import AuditLog, ChainBrokenError
from ai_banking_copilot.policy.audit_validation import verify_for_service
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.breaker_store import StoreBreakerStateStore, init_breaker_schema
from ai_banking_copilot.policy.pep import Pep
from ai_banking_copilot.policy.reversal import ReversalService
from ai_banking_copilot.policy.verification import (
    DemoVerificationProvider,
    ExternalVerificationProvider,
    HmacStrongVerificationProofVerifier,
    StoreVerificationAttemptStateStore,
    VerificationAttemptLimiter,
    VerificationMode,
    VerificationProvider,
    init_verification_schema,
)
from ai_banking_copilot.scenarios.registry import QUERY_HANDLERS, WRITE_HANDLERS
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.web.app import (
    AUTH_MODE_DEMO,
    AUTH_MODE_ENV,
    AUTH_MODE_OFF,
    AppDeps,
)
from ai_banking_copilot.web.auth import DEMO_ACCOUNTS, AuthService, init_auth_schema
from ai_banking_copilot.web.bill_narrative import LlmBillNarrator
from ai_banking_copilot.web.channels.identity import init_channel_schema
from ai_banking_copilot.web.conversation_store import (
    ConversationStore,
    init_conversation_schema,
)
from ai_banking_copilot.web.insights import InsightService, SQLiteInsightMetadataStore
from ai_banking_copilot.web.life_event_store import LifeEventStore, init_life_event_schema
from ai_banking_copilot.web.session_repository import SQLiteSessionRepository, init_session_schema

DISABLE_LLM_ENV = "AIB_DISABLE_LLM"
"""置为真值时强制**只用模板通道**（现场演示保稳的可选开关）。"""

_DISABLED_VALUES = frozenset({"1", "true", "yes", "on"})

REVIEW_CHANNEL_ENV = "AIB_REVIEW_CHANNEL"
REVIEW_REQUIRED_ENV = "AIB_REVIEW_REQUIRED"
INSIGHT_DB_ENV = "AIB_INSIGHT_DB"

SESSION_RISK_STRICT_ENV = "AIB_SESSION_RISK_STRICT"
"""会话风控严格模式（G-03）：置为真值时，缺少可信风控结果即升级核验（fail-closed）。"""

_KEY_ENV_NAMES = ("AI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY")
"""凭据环境变量（按序取首个非空）：通用 `AI_*` 优先，兼容本仓库历史 `DASHSCOPE_API_KEY`。"""


def build_intent_model(
    env: Mapping[str, str] | None = None, *, config: ModelConfig | None = None,
    observer: CallObserver | None = None,
) -> IntentModel:
    """装配意图通道：默认"真模型优先 + 失败降级模板"；`AIB_DISABLE_LLM` 真值时只用模板。

    端点与模型可经 `AI_BASE_URL`（或 `OPENAI_API_BASE_URL`）/ `AI_MODEL` 覆盖，
    未提供时沿用 `LlmIntentModel` 的默认值。
    """
    settings = config if config is not None else ModelConfig.from_env(env)
    if settings.disable_llm:
        return TemplateIntentModel()
    return FallbackIntentModel(LlmIntentModel(
        config=settings, observer=observer, prompt_version="planner-v1",
    ))


def build_reviewer(
    env: Mapping[str, str] | None = None, *, config: ModelConfig | None = None,
    observer: CallObserver | None = None,
) -> Reviewer:
    """装配计划后置审查；默认是明确标注的旁路模板诊断。"""
    source = os.environ if env is None else env
    settings = config if config is not None else ModelConfig.from_env(source)
    mode = source.get(REVIEW_CHANNEL_ENV, ReviewChannel.BYPASS.value).strip().lower()
    if settings.disable_llm:
        mode = ReviewChannel.BYPASS.value
    if mode == ReviewChannel.LLM_STRICT.value and config is not None:
        return StrictPlanReviewer(config=settings, observer=observer)
    timeout = settings.reviewer_timeout_s
    if timeout != DEFAULT_REVIEWER_TIMEOUT_S:
        return create_reviewer(
            mode, api_key=settings.api_key, model=settings.reviewer_model,
            endpoint=settings.endpoint, timeout_s=timeout,
        )
    return create_reviewer(
        mode, api_key=settings.api_key, model=settings.reviewer_model, endpoint=settings.endpoint,
    )


def reviewer_required(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    if source.get(DISABLE_LLM_ENV, "").strip().lower() in _DISABLED_VALUES:
        return False
    if source.get(REVIEW_CHANNEL_ENV, "").strip().lower() == ReviewChannel.LLM_STRICT.value:
        return True
    return source.get(REVIEW_REQUIRED_ENV, "").strip().lower() in _DISABLED_VALUES


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
    if not verify_for_service(audit):
        raise ChainBrokenError(
            f"审计链校验失败：{audit_path}（拒绝损坏、密钥不匹配或未经显式迁移的 SHA 历史）"
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


def build_session_risk_strict(env: Mapping[str, str] | None = None) -> bool:
    """会话风控严格模式（G-03）。

    演示装配（本地单用户、无设备 / 地点遥测可比对）显式放开；生产装配应置
    `AIB_SESSION_RISK_STRICT` 为真值，使「无可信风控结果」升级核验而非默认放行。
    """
    source = os.environ if env is None else env
    return source.get(SESSION_RISK_STRICT_ENV, "").strip().lower() in _DISABLED_VALUES


DEMO_PASSWORD_ENV = "AIB_DEMO_PASSWORD"
"""演示账号口令（仅 `AIB_AUTH_MODE=demo` 生效）；未配置时用内置演示口令。"""

_DEFAULT_DEMO_PASSWORD = "aib-demo-1027"


def build_auth_mode(env: Mapping[str, str] | None = None) -> str:
    """登录模式：**默认 `off`**（单测基线沿用单用户）；部署 / 验收显式设 `demo`。"""

    source = os.environ if env is None else env
    mode = source.get(AUTH_MODE_ENV, AUTH_MODE_OFF).strip().lower()
    return AUTH_MODE_DEMO if mode == AUTH_MODE_DEMO else AUTH_MODE_OFF


def _verify_demo_accounts(auth: AuthService) -> None:
    """启动自检：演示账号必须都能解析到用户，缺一即拒绝启动（早死早发现）。"""

    missing = [row[0] for row in DEMO_ACCOUNTS if auth.user_by_username(row[0]) is None]
    if missing:
        raise RuntimeError(f"演示账号缺失：{'、'.join(missing)}")


def build_bill_narrator(
    env: Mapping[str, str] | None = None, *, config: ModelConfig | None = None,
    observer: CallObserver | None = None,
) -> LlmBillNarrator | None:
    """装配账单结论式解释通道：`AIB_DISABLE_LLM` 或无凭据时返回 `None`（确定性降级）。

    识别与金额核算不依赖本通道；装配缺失只会让解释退化为规则文案，并显式标注降级。
    """
    settings = config if config is not None else ModelConfig.from_env(env)
    if settings.disable_llm or not settings.api_key:
        return None
    return LlmBillNarrator(config=settings, observer=observer)


def build_verification(
    db_path: Path, env: Mapping[str, str] | None = None,
) -> tuple[VerificationProvider, bytes | None, bool]:
    """强验证密钥独立于审计密钥；external 缺密钥时由申请入口失败关闭。"""
    source = os.environ if env is None else env
    mode = VerificationMode(source.get("AIB_VERIFICATION_MODE", "demo").strip().lower())
    strict = source.get("AIB_VERIFICATION_STRICT", "").strip().lower() in _DISABLED_VALUES
    key = source.get("AIB_VERIFICATION_KEY", "").strip()
    if mode is VerificationMode.EXTERNAL:
        return ExternalVerificationProvider(
            endpoint=source.get("AIB_VERIFICATION_ENDPOINT", "").strip() or None,
            token=source.get("AIB_VERIFICATION_TOKEN", "").strip() or None,
            target=source.get("AIB_VERIFICATION_TARGET", "").strip() or None,
        ), key.encode() if key else None, strict
    if not key and not strict:
        key_path = db_path.with_suffix(".verification.key")
        if key_path.is_file():
            key = key_path.read_text(encoding="utf-8").strip()
        else:
            key = secrets.token_hex(32)
            key_path.write_text(key, encoding="utf-8")
    return DemoVerificationProvider(), key.encode() if key else None, strict


def build_deps(
    db_path: Path, env: Mapping[str, str] | None = None, *, resources: ExitStack | None = None
) -> AppDeps:
    """装配默认依赖；短期评测可传资源栈，在用例结束时关闭数据库连接。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    source = os.environ if env is None else env
    writer = Store(db_path)
    init_schema(writer)
    seed_database(writer)
    init_risk_schema(writer)
    seed_risk_assessment(writer)
    init_merchant_schema(writer)
    seed_user_profiles(writer)
    seed_secondary_user(writer)
    init_transfer_schema(writer)
    init_breaker_schema(writer)
    init_session_schema(writer)
    init_channel_schema(writer)
    init_auth_schema(writer)
    init_conversation_schema(writer)
    init_life_event_schema(writer)
    writer.close()

    read_store = Store(db_path, StoreMode.READ_ONLY)
    if resources is not None:
        resources.callback(read_store.close)
    gateway = FactGateway(read_store)
    registry = build_default_registry()
    breaker, _breaker_store = build_breaker(db_path, env)
    if resources is not None and _breaker_store is not None:
        resources.callback(_breaker_store.close)
    audit = build_audit_log(db_path, env)
    core_store = Store(db_path, StoreMode.READ_WRITE)
    if resources is not None:
        resources.callback(core_store.close)
    core_bank = CoreBank(core_store)
    session_store = Store(db_path, StoreMode.READ_WRITE)
    if resources is not None:
        resources.callback(session_store.close)
    activity_store = Store(db_path, StoreMode.READ_WRITE)
    if resources is not None:
        resources.callback(activity_store.close)
    auth_mode = build_auth_mode(env)
    auth_service = AuthService(activity_store)
    if auth_mode == AUTH_MODE_DEMO:
        password = source.get(DEMO_PASSWORD_ENV, "").strip() or _DEFAULT_DEMO_PASSWORD
        auth_service.ensure_demo_accounts(password)
        _verify_demo_accounts(auth_service)
    risk_store = Store(db_path, StoreMode.READ_WRITE)
    if resources is not None:
        resources.callback(risk_store.close)
    verification_provider, verification_key, verification_strict = build_verification(db_path, env)
    verification_store = Store(db_path, StoreMode.READ_WRITE)
    init_verification_schema(verification_store)
    if resources is not None:
        resources.callback(verification_store.close)
    verification_limiter = VerificationAttemptLimiter(
        store=StoreVerificationAttemptStateStore(verification_store)
    )
    model_config = ModelConfig.from_env(source)

    def analysis_code_generator(task: str, fields: tuple[str, ...]) -> str:
        return generate_analysis_code(AnalysisTask(task), fields, config=model_config)

    model_observations = TaskObservations()
    insight_path = Path(
        source.get(INSIGHT_DB_ENV, "").strip()
        or db_path.with_name(f"{db_path.stem}-insights.db")
    )
    insight_metadata = SQLiteInsightMetadataStore(insight_path)
    if resources is not None:
        resources.callback(insight_metadata.close)

    @contextmanager
    def read_gateway_factory() -> Iterator[FactGateway]:
        store = Store(db_path, StoreMode.READ_ONLY)
        try:
            yield FactGateway(store)
        finally:
            store.close()

    return AppDeps(
        intent_model=build_intent_model(env, config=model_config, observer=model_observations),
        registry=registry,
        gateway=gateway,
        pep=Pep(
            registry, gateway, core_bank, audit, breaker, WRITE_HANDLERS, QUERY_HANDLERS,
            strong_verification=(
                HmacStrongVerificationProofVerifier(verification_key)
                if verification_key else None
            ),
            analysis_code_generator=analysis_code_generator,
        ),
        breaker=breaker,
        audit=audit,
        user_id=DEMO_USER_ID,
        risk=StoreRiskAssessmentSource(read_store),
        risk_writer=StoreRiskAssessmentWriter(risk_store),
        reversal=ReversalService(core_bank),
        operator_token=build_operator_token(env),
        reviewer=build_reviewer(env, config=model_config, observer=model_observations),
        reviewer_required=reviewer_required(env),
        insights=InsightService(read_gateway_factory, insight_metadata),
        verification_provider=verification_provider,
        verification_key=verification_key,
        verification_strict=verification_strict,
        verification_limiter=verification_limiter,
        session_repository=SQLiteSessionRepository(session_store),
        channel_store=session_store,
        auth=auth_service,
        auth_mode=auth_mode,
        conversations=ConversationStore(activity_store),
        life_events=LifeEventStore(activity_store),
        receipts=StoreReceiptReader(read_store),
        model_observations=model_observations,
        session_risk_strict=build_session_risk_strict(env),
        bill_narrator=build_bill_narrator(
            env, config=model_config, observer=model_observations,
        ),
    )
