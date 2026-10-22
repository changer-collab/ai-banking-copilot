"""web 渠道层测试共享夹具：装配完整双核链路（与 03 / 05 同风格），全程不联网。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE, CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.risk_store import (
    StoreRiskAssessmentSource,
    StoreRiskAssessmentWriter,
    init_risk_schema,
    seed_risk_assessment,
)
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.planning.llm import TemplateIntentModel
from ai_banking_copilot.policy.audit import AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.pep import Pep
from ai_banking_copilot.policy.reversal import ReversalService
from ai_banking_copilot.scenarios.registry import QUERY_HANDLERS, WRITE_HANDLERS
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.tools.registry import ToolRegistry
from ai_banking_copilot.web.app import AppDeps


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """建库 + 播种后的数据库文件（种子数据可复现）。"""
    path = tmp_path / "web.db"
    store = Store(path)
    init_schema(store)
    seed_database(store)
    init_risk_schema(store)
    seed_risk_assessment(store)
    store.close()
    return path


@pytest.fixture
def rw_store(db_path: Path) -> Iterator[Store]:
    """读写连接（TCB 写面；PEP 经 CoreBank 写入）。"""
    store = Store(db_path, StoreMode.READ_WRITE)
    yield store
    store.close()


@pytest.fixture
def gateway(db_path: Path) -> Iterator[FactGateway]:
    """只读事实网关（计划卡取值 + PEP 出处校验）。"""
    store = Store(db_path, StoreMode.READ_ONLY)
    yield FactGateway(store)
    store.close()


@pytest.fixture
def risk(db_path: Path) -> Iterator[StoreRiskAssessmentSource]:
    """受信任风险测评事实源（只读）。"""
    store = Store(db_path, StoreMode.READ_ONLY)
    yield StoreRiskAssessmentSource(store)
    store.close()


@pytest.fixture
def risk_writer(rw_store: Store) -> StoreRiskAssessmentWriter:
    """风险测评**录入通道**（写连接；`source='user_input'`）。"""
    return StoreRiskAssessmentWriter(rw_store)


@pytest.fixture
def core_bank(rw_store: Store) -> CoreBank:
    """模拟核心银行写原语（与 rw_store 共用同一连接）。"""
    return CoreBank(rw_store)


@pytest.fixture
def registry() -> ToolRegistry:
    """README 规范动作表 20 个动作的白名单。"""
    return build_default_registry()


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    """独立 JSONL 审计链（不占用 01 的 SQLite schema）。"""
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def breaker() -> CircuitBreaker:
    """默认阈值（连续 3 次授权失败即熔断）的熔断器。"""
    return CircuitBreaker()


@pytest.fixture
def pep(
    registry: ToolRegistry,
    gateway: FactGateway,
    core_bank: CoreBank,
    audit: AuditLog,
    breaker: CircuitBreaker,
) -> Pep:
    """策略执行点：本层唯一的执行通道。"""
    return Pep(registry, gateway, core_bank, audit, breaker, WRITE_HANDLERS, QUERY_HANDLERS)


@pytest.fixture
def deps(
    registry: ToolRegistry,
    gateway: FactGateway,
    pep: Pep,
    breaker: CircuitBreaker,
    audit: AuditLog,
    risk: StoreRiskAssessmentSource,
    risk_writer: StoreRiskAssessmentWriter,
    core_bank: CoreBank,
) -> AppDeps:
    """渠道层依赖装配（默认 LLM-off：模板通道）。"""
    return AppDeps(
        intent_model=TemplateIntentModel(),
        registry=registry,
        gateway=gateway,
        pep=pep,
        breaker=breaker,
        audit=audit,
        user_id=DEMO_USER_ID,
        today=DEFAULT_BOOKING_DATE,
        risk=risk,
        risk_writer=risk_writer,
        reversal=ReversalService(core_bank),
        session_risk_strict=False,
    )


class _FixedIntentModel:
    """固定意图通道：返回调用方给定的意图（用于构造多步 / 长尾 Demo，不联网）。"""

    def __init__(self, intents: tuple[object, ...]) -> None:
        self._intents = intents

    def propose(self, utterance: str, context: object) -> object:
        from ai_banking_copilot.planning.llm import IntentProposal

        return IntentProposal(intents=self._intents, raw=utterance)  # type: ignore[arg-type]


@pytest.fixture
def fixed_intent_model() -> object:
    """默认返回空意图的固定通道夹具（具体意图由用例自行构造）。"""
    return _FixedIntentModel(())
