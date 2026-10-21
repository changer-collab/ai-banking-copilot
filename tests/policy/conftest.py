"""policy 测试共享夹具：复用 01 的 Store / init_schema / seed_database。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ai_banking_copilot.contracts.taint import SlotKind
from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.policy.audit import AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.pep import Pep
from ai_banking_copilot.scenarios.registry import QUERY_HANDLERS, WRITE_HANDLERS
from ai_banking_copilot.tools.registry import ParamSpec, RiskClass, ToolRegistry, ToolSpec


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """建库 + 播种后的数据库文件（种子数据可复现）。"""
    path = tmp_path / "demo.db"
    store = Store(path)
    init_schema(store)
    seed_database(store)
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
    """只读事实网关（PEP 的出处校验与查询路径）。"""
    store = Store(db_path, StoreMode.READ_ONLY)
    yield FactGateway(store)
    store.close()


@pytest.fixture
def core_bank(rw_store: Store) -> CoreBank:
    """模拟核心银行写原语（与 rw_store 共用同一连接）。"""
    return CoreBank(rw_store)


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    """独立 JSONL 审计链（**不占用 01 的 SQLite schema**）。"""
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def breaker() -> CircuitBreaker:
    """默认阈值（连续 3 次授权失败即熔断）的熔断器。"""
    return CircuitBreaker()


def _param(name: str, py_type: type[object], required: bool = True) -> ParamSpec:
    """控制槽参数（控制槽只能由确定性解析器填充，故一律 CONTROL）。"""
    return ParamSpec(name=name, slot=SlotKind.CONTROL, py_type=py_type, required=required)


def _spec(
    action: str,
    risk: RiskClass,
    params: tuple[ParamSpec, ...],
    *,
    pure: bool,
    reversible: bool = True,
    money: str | None = None,
) -> ToolSpec:
    return ToolSpec(
        action=action,
        risk=risk,
        params=params,
        is_pure_query=pure,
        reversible=reversible,
        money_param=money,
        description=f"{action}（测试用工具契约）",
    )


def _build_test_registry() -> ToolRegistry:
    """最小工具白名单：动作名**与 README 规范动作表逐项一致（全 20 个）**，覆盖纯查询 + 全部写动作
    （自建注册表，不依赖 02 的 catalog，保证 03 可独立验收）。

    写处理器注入生产场景模块自带的那一份（`scenarios.registry.WRITE_HANDLERS`）——03 的
    执行分支已改为 handler 注册表（`docs/已修复清单-2026-10-08.md` 步骤 1.5），
    故不再在测试里另写一套平行表。
    """
    registry = ToolRegistry()
    for spec in (
        _spec("account.balance", RiskClass.READ_ONLY, (_param("account_id", str),), pure=True),
        _spec(
            "account.transactions",
            RiskClass.READ_ONLY,
            (_param("account_id", str), _param("start", str), _param("end", str)),
            pure=True,
        ),
        _spec(
            "bills.monthly",
            RiskClass.READ_ONLY,
            (_param("user_id", str), _param("year", int), _param("month", int)),
            pure=True,
        ),
        _spec(
            "bills.category_trend",
            RiskClass.READ_ONLY,
            (
                _param("user_id", str),
                _param("category", str),
                _param("base_year", int),
                _param("base_month", int),
            ),
            pure=True,
        ),
        _spec("wealth.list", RiskClass.READ_ONLY, (), pure=True),
        _spec("card.list", RiskClass.READ_ONLY, (_param("user_id", str),), pure=True),
        _spec("subscription.list", RiskClass.READ_ONLY, (_param("user_id", str),), pure=True),
        _spec(
            "transfer.create",
            RiskClass.LARGE_FUNDS,
            (
                _param("user_id", str),
                _param("account_id", str),
                _param("payee_id", str),
                _param("amount_cents", int),
                _param("booked_on", str, required=False),
            ),
            pure=False,
            money="amount_cents",
        ),
        _spec(
            "transfer.schedule",
            RiskClass.LARGE_FUNDS,
            (
                _param("user_id", str),
                _param("account_id", str),
                _param("payee_id", str),
                _param("amount_cents", int),
                _param("booked_on", str, required=False),
            ),
            pure=False,
            money="amount_cents",
        ),
        _spec(
            "wealth.subscribe",
            RiskClass.LARGE_FUNDS,
            (
                _param("user_id", str),
                _param("account_id", str),
                _param("product_id", str),
                _param("amount_cents", int),
            ),
            pure=False,
            money="amount_cents",
        ),
        _spec(
            "wealth.redeem",
            RiskClass.LARGE_FUNDS,
            (
                _param("user_id", str),
                _param("account_id", str),
                _param("product_id", str),
                _param("amount_cents", int),
            ),
            pure=False,
            money="amount_cents",
        ),
        _spec(
            "subscription.cancel",
            RiskClass.SMALL_CHANGE,
            (_param("user_id", str), _param("subscription_id", str)),
            pure=False,
        ),
        _spec(
            "subscription.sign",
            RiskClass.SMALL_CHANGE,
            (
                _param("user_id", str),
                _param("merchant", str),
                _param("amount_cents", int),
                _param("period", str),
                _param("next_charge_on", str),
            ),
            pure=False,
            money="amount_cents",
        ),
        _spec("card.lock", RiskClass.SMALL_CHANGE, (_param("card_id", str),), pure=False),
        _spec("card.unlock", RiskClass.SMALL_CHANGE, (_param("card_id", str),), pure=False),
        _spec(
            "card.report_lost",
            RiskClass.IRREVERSIBLE,
            (_param("card_id", str), _param("reason", str, required=False)),
            pure=False,
            reversible=False,
        ),
        _spec(
            "card.apply_virtual",
            RiskClass.SMALL_CHANGE,
            (_param("user_id", str), _param("daily_limit_cents", int)),
            pure=False,
            money="daily_limit_cents",
        ),
        _spec(
            "card.set_limit",
            RiskClass.SMALL_CHANGE,
            (_param("card_id", str), _param("daily_limit_cents", int)),
            pure=False,
            money="daily_limit_cents",
        ),
        _spec(
            "funds.freeze",
            RiskClass.SMALL_CHANGE,
            (_param("account_id", str), _param("amount_cents", int), _param("reason", str)),
            pure=False,
            money="amount_cents",
        ),
        _spec("funds.release", RiskClass.SMALL_CHANGE, (_param("hold_id", str),), pure=False),
    ):
        registry.register(spec)
    return registry


@pytest.fixture
def registry() -> ToolRegistry:
    return _build_test_registry()


@pytest.fixture
def pep(
    registry: ToolRegistry,
    gateway: FactGateway,
    core_bank: CoreBank,
    audit: AuditLog,
    breaker: CircuitBreaker,
) -> Pep:
    return Pep(registry, gateway, core_bank, audit, breaker, WRITE_HANDLERS, QUERY_HANDLERS)
