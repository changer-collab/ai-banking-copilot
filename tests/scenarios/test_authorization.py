"""G3 交叉断言：评测链与产品链共用节点裁决请求构造器（口径单源）。

镜像 `tests/web/conftest.py` 的双核装配（同一 DB 上只读网关 + 读写写原语），
先转出 ¥2000 抬高当日累计，再在代表性节点上断言
`Session._node_decision` 与 `EvalPipeline._evaluate_node` 的 level + reasons 完全相等。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.contracts.plan import AnchorSource, IntentRef, Plan, PlanNode
from ai_banking_copilot.eval.pipeline import EvalPipeline
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE, CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.risk_store import (
    StoreRiskAssessmentSource,
    init_risk_schema,
    seed_risk_assessment,
)
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.planning.llm import TemplateIntentModel
from ai_banking_copilot.policy.audit import AuditLog
from ai_banking_copilot.policy.breaker import CircuitBreaker
from ai_banking_copilot.policy.pdp import ObjectState, evaluate
from ai_banking_copilot.policy.pep import Pep
from ai_banking_copilot.policy.verification import HmacStrongVerificationProofVerifier
from ai_banking_copilot.scenarios.authorization import node_policy_request, object_state_for_node
from ai_banking_copilot.scenarios.registry import QUERY_HANDLERS, WRITE_HANDLERS
from ai_banking_copilot.tools.catalog import build_default_registry
from ai_banking_copilot.tools.registry import ToolRegistry
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session

_TEST_VERIFICATION_KEY = b"authorization-test-verification-key"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """建库 + 播种 + 风险测评的数据库文件（种子数据可复现）。"""
    path = tmp_path / "authorization.db"
    store = Store(path)
    init_schema(store)
    seed_database(store)
    init_risk_schema(store)
    seed_risk_assessment(store)
    store.close()
    return path


@pytest.fixture
def rw_store(db_path: Path) -> Iterator[Store]:
    """读写连接（TCB 写面；CoreBank / PEP 经此写入）。"""
    store = Store(db_path, StoreMode.READ_WRITE)
    yield store
    store.close()


@pytest.fixture
def gateway(db_path: Path) -> Iterator[FactGateway]:
    """只读事实网关（与读写连接共享同一 DB 文件）。"""
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
def core_bank(rw_store: Store) -> CoreBank:
    """模拟核心银行写原语（与 rw_store 共用同一连接）。"""
    return CoreBank(rw_store)


@pytest.fixture
def registry() -> ToolRegistry:
    """README 规范动作表 20 个动作的白名单。"""
    return build_default_registry()


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    """独立 JSONL 审计链。"""
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def breaker() -> CircuitBreaker:
    """默认阈值的熔断器。"""
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
    return Pep(
        registry,
        gateway,
        core_bank,
        audit,
        breaker,
        WRITE_HANDLERS,
        QUERY_HANDLERS,
        strong_verification=HmacStrongVerificationProofVerifier(_TEST_VERIFICATION_KEY),
    )


@pytest.fixture
def deps(
    registry: ToolRegistry,
    gateway: FactGateway,
    pep: Pep,
    breaker: CircuitBreaker,
    audit: AuditLog,
    risk: StoreRiskAssessmentSource,
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
        verification_key=_TEST_VERIFICATION_KEY,
        today=DEFAULT_BOOKING_DATE,
        risk=risk,
        session_risk_strict=False,
    )


def _node(node_id: str, action: str, params: dict[str, object]) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        action=action,
        params=params,
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id=f"anchor_{node_id}"),
    )


def _representative_nodes() -> tuple[PlanNode, ...]:
    return (
        _node(
            "t300",
            "transfer.create",
            {
                "account_id": "acc_savings",
                "payee_id": "p_lina",
                "amount_cents": 30000,
                "booked_on": "2026-10-01",
            },
        ),
        _node(
            "t3000",
            "transfer.create",
            {
                "account_id": "acc_savings",
                "payee_id": "p_lina",
                "amount_cents": 300000,
                "booked_on": "2026-10-01",
            },
        ),
        _node(
            "subscribe",
            "wealth.subscribe",
            {"account_id": "acc_savings", "product_id": "wp_cash", "amount_cents": 10000},
        ),
        _node(
            "redeem",
            "wealth.redeem",
            {"account_id": "acc_savings", "product_id": "wp_cash", "amount_cents": 10000},
        ),
        _node(
            "freeze",
            "funds.freeze",
            {"account_id": "acc_savings", "amount_cents": 10000, "reason": "预算锁定"},
        ),
        _node("cancel", "subscription.cancel", {"subscription_id": "sub_video"}),
        _node("lock", "card.lock", {"card_id": "card_debit"}),
        _node("unlock", "card.unlock", {"card_id": "card_debit"}),
        _node("lost", "card.report_lost", {"card_id": "card_credit"}),
        _node("virtual", "card.apply_virtual", {"daily_limit_cents": 50000}),
        _node(
            "set_limit",
            "card.set_limit",
            {"card_id": "card_debit", "daily_limit_cents": 500000},
        ),
        _node("bills", "bills.monthly", {"year": 2026, "month": 9}),
    )


def test_object_state_for_node_covers_five_states(gateway: FactGateway) -> None:
    self_state = _node("s", "card.lock", {"card_id": "card_debit"})
    blocked = _node("b", "transfer.create", {"payee_id": "p_zhaoliu"})
    verified = _node("v", "transfer.create", {"payee_id": "p_lina"})
    known = _node("k", "transfer.create", {"payee_id": "p_wangwei_b"})
    unknown = _node("u", "transfer.create", {"payee_id": "p_nope"})

    assert object_state_for_node(gateway, self_state) is ObjectState.SELF
    assert object_state_for_node(gateway, blocked) is ObjectState.BLOCKED
    assert object_state_for_node(gateway, verified) is ObjectState.VERIFIED_SAVED
    assert object_state_for_node(gateway, known) is ObjectState.KNOWN_UNVERIFIED
    assert object_state_for_node(gateway, unknown) is ObjectState.UNKNOWN


def test_object_state_covers_the_payer_of_a_collection_request(gateway: FactGateway) -> None:
    """P2-3 裁决：**收款**请求的对手方是付款方（`payer_id`），与转出对象同口径处置。

    阻断名册若只认 `payee_id`，等于给被冻结 / 涉诈的对手方留了一条**资金入口**。
    """
    assert (
        object_state_for_node(
            gateway, _node("pc", "transfer.request_collection", {"payer_id": "p_zhaoliu"})
        )
        is ObjectState.BLOCKED
    )
    assert (
        object_state_for_node(
            gateway, _node("pv", "transfer.request_collection", {"payer_id": "p_lina"})
        )
        is ObjectState.VERIFIED_SAVED
    )
    assert (
        object_state_for_node(
            gateway, _node("pk", "transfer.request_collection", {"payer_id": "p_wangwei_b"})
        )
        is ObjectState.KNOWN_UNVERIFIED
    )
    assert (
        object_state_for_node(
            gateway, _node("px", "transfer.request_collection", {"payer_id": "p_nope"})
        )
        is ObjectState.UNKNOWN
    )


def test_blocked_payer_makes_the_collection_node_a_deny(
    gateway: FactGateway, registry: ToolRegistry
) -> None:
    """端到端口径：PDP 对 `blocked` 付款方的收款节点直接判 `DENY`（不再只靠写层兜底）。"""
    node = _node("pc", "transfer.request_collection", {"payer_id": "p_zhaoliu"})
    request = node_policy_request(
        node,
        spec=registry.get(node.action),
        gateway=gateway,
        user_id=DEMO_USER_ID,
        today=DEFAULT_BOOKING_DATE,
        flags=frozenset(),
    )
    assert request.object_state is ObjectState.BLOCKED
    assert evaluate(request).level is Level.DENY


def test_node_policy_request_outflow_takes_amount_and_baseline(
    gateway: FactGateway, registry: ToolRegistry
) -> None:
    node = _node(
        "t",
        "transfer.create",
        {
            "account_id": "acc_savings",
            "payee_id": "p_lina",
            "amount_cents": 50000,
            "booked_on": "2026-10-01",
        },
    )
    request = node_policy_request(
        node,
        spec=registry.get("transfer.create"),
        gateway=gateway,
        user_id=DEMO_USER_ID,
        today=DEFAULT_BOOKING_DATE,
        flags=frozenset(),
    )
    assert request.amount_applies is True
    assert request.amount == Money(50000)
    assert request.daily_accumulated == Money(50000)
    assert request.object_state is ObjectState.VERIFIED_SAVED
    assert request.is_pure_query is False
    assert request.irreversible is False


def test_node_policy_request_non_outflow_ignores_daily_limit_param(
    gateway: FactGateway, registry: ToolRegistry
) -> None:
    node = _node(
        "sl", "card.set_limit", {"card_id": "card_debit", "daily_limit_cents": 500000}
    )
    request = node_policy_request(
        node,
        spec=registry.get("card.set_limit"),
        gateway=gateway,
        user_id=DEMO_USER_ID,
        today=DEFAULT_BOOKING_DATE,
        flags=frozenset(),
    )
    assert request.amount_applies is False
    assert request.amount == Money(0)
    assert request.daily_accumulated == Money(0)


def test_web_and_eval_agree_on_representative_nodes(
    deps: AppDeps,
    gateway: FactGateway,
    core_bank: CoreBank,
    pep: Pep,
    tmp_path: Path,
) -> None:
    core_bank.transfer(
        user_id=DEMO_USER_ID,
        account_id="acc_savings",
        payee_id="p_lina",
        amount=Money(200000),
        idempotency_key="seed-2000",
        booked_on=DEFAULT_BOOKING_DATE,
    )
    assert gateway.daily_transfer_total(DEMO_USER_ID, DEFAULT_BOOKING_DATE).cents == 200000

    session = Session(deps)
    pipeline = EvalPipeline(tmp_path / "eval")

    levels: dict[str, Level] = {}
    for node in _representative_nodes():
        web_decision = session._node_decision(node, frozenset())
        _, eval_decision = pipeline._evaluate_node(
            node,
            gateway=gateway,
            flags=frozenset(),
            pep=pep,
            case_id="x",
            plan=Plan(plan_id="consistency", nodes=(node,)),
        )
        assert eval_decision is not None
        assert (web_decision.level, web_decision.reasons) == (
            eval_decision.level,
            eval_decision.reasons,
        )
        levels[node.node_id] = web_decision.level

    # 调高限额 = 解除既有风控上限：2026-10-26 起为官方红（不随金额档降级）。
    assert levels["set_limit"] is Level.RED
    assert levels["lock"] is Level.YELLOW
    assert levels["t3000"] is Level.RED
    assert levels["bills"] is Level.GREEN
