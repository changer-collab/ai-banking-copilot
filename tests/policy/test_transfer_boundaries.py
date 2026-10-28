"""转账四动作的三色边界（红 / 黄）——**红色动作测试的样板**。

口径唯一来源：`docs/2026-10-26/三色基准表-2026-10-26.md`（与 `policy/pdp.py` 不一致时以表为准）。

| 动作 | 金额档 | 口径 |
|---|---|---|
| `transfer.create` | 适用（`OUTFLOW`） | 日累计（**含本笔**）≤ 1000 ⇒ 黄；> 1000 ⇒ 红 |
| `transfer.schedule` | 适用（`OUTFLOW`） | 立即扣款，口径同 `create` |
| `transfer.book` | **不适用**（`STATE`） | 登记不扣款 ⇒ 恒黄；到期派发为 `create` 时再按累计裁决 |
| `transfer.request_collection` | 不适用 | 官方点名红色 ⇒ 无条件红（金额再小也不降档） |

**为什么这样写**（样板要点，供其他场景照抄）：

1. 断言对象是 `scenarios.authorization.node_policy_request` + `policy.pdp.evaluate`，
   也就是**产品链与评测链共用的同一入口**；不手搓 `PolicyRequest`，避免口径分叉。
2. 金额一律用**分**（`Money(cents)`），阈值两侧各取一笔（999 / 1000 / 1001），
   **恰好等于 1000 必须为黄**——官方是「≤」，不是「<」。
3. 日累计一律走真实网关 `daily_transfer_total`（真写库再读），不用假数据打桩；
   `transfer.book` 必须能被证明「登记后不抬高当日累计」。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ai_banking_copilot.contracts.authorization import Level, ReasonCode
from ai_banking_copilot.contracts.plan import AnchorSource, IntentRef, PlanNode
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE, CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.policy.pdp import DAILY_LIMIT_YUAN, evaluate
from ai_banking_copilot.scenarios.authorization import node_policy_request
from ai_banking_copilot.tools.catalog import build_default_registry

_ACCOUNT = "acc_savings"
_PAYEE = "p_lina"
"""已确认身份的收款人（`identity_confirmed=1`），避免「陌生对象 +黄」干扰金额档断言。"""

_AT_LIMIT = DAILY_LIMIT_YUAN * 100
"""恰好等于官方日限额的分值（1000 元 = 100000 分）——**必须判黄**。"""


@dataclass(frozen=True)
class _Outcome:
    level: Level
    reasons: tuple[ReasonCode, ...]
    amount_applies: bool
    daily_accumulated_cents: int


def _node(action: str, cents: int) -> PlanNode:
    params: dict[str, object] = {"amount_cents": cents}
    if action == "transfer.request_collection":
        params["payer_id"] = _PAYEE
    else:
        params.update(
            {
                "account_id": _ACCOUNT,
                "payee_id": _PAYEE,
                "booked_on": DEFAULT_BOOKING_DATE.isoformat(),
            }
        )
    return PlanNode(
        node_id="n1",
        action=action,
        params=params,
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id="a1"),
    )


def _decide(gateway: FactGateway, action: str, cents: int) -> _Outcome:
    """按产品 / 评测链同一入口组装裁决请求并裁决（只读网关）。"""
    request = node_policy_request(
        _node(action, cents),
        spec=build_default_registry().get(action),
        gateway=gateway,
        user_id=DEMO_USER_ID,
        today=DEFAULT_BOOKING_DATE,
        flags=frozenset(),
    )
    decision = evaluate(request)
    return _Outcome(
        level=decision.level,
        reasons=decision.reasons,
        amount_applies=request.amount_applies,
        daily_accumulated_cents=request.daily_accumulated.cents,
    )


def _spend(core_bank: CoreBank, cents: int, key: str) -> None:
    """真实转出一笔（计入当日累计），用于构造累计场景。"""
    core_bank.transfer(
        user_id=DEMO_USER_ID,
        account_id=_ACCOUNT,
        payee_id=_PAYEE,
        amount=Money(cents),
        idempotency_key=key,
        booked_on=DEFAULT_BOOKING_DATE,
    )


@pytest.mark.parametrize("action", ["transfer.create", "transfer.schedule"])
@pytest.mark.parametrize(
    ("cents", "expected", "reason"),
    [
        (999_00, Level.YELLOW, ReasonCode.STATE_CHANGE_SMALL),
        (_AT_LIMIT, Level.YELLOW, ReasonCode.STATE_CHANGE_SMALL),
        (1001_00, Level.RED, ReasonCode.AMOUNT_OVER_DAILY_LIMIT),
    ],
)
def test_amount_band_for_outflow_transfers(
    gateway: FactGateway,
    action: str,
    cents: int,
    expected: Level,
    reason: ReasonCode,
) -> None:
    """999 / 1000 / 1001 三档：等于 1000 仍是黄（官方口径是「≤」）。"""
    outcome = _decide(gateway, action, cents)
    assert outcome.amount_applies is True
    assert outcome.daily_accumulated_cents == cents
    assert outcome.level is expected
    assert reason in outcome.reasons


def test_daily_accumulation_includes_this_transfer(
    gateway: FactGateway, core_bank: CoreBank
) -> None:
    """日累计口径：**已转 900 + 本笔 200 = 1100 ⇒ 红**；累计恰好 1000 ⇒ 黄。"""
    _spend(core_bank, 900_00, "k900")

    over = _decide(gateway, "transfer.create", 200_00)
    assert over.daily_accumulated_cents == 1100_00
    assert over.level is Level.RED
    assert ReasonCode.AMOUNT_OVER_DAILY_LIMIT in over.reasons

    at = _decide(gateway, "transfer.create", 100_00)
    assert at.daily_accumulated_cents == _AT_LIMIT
    assert at.level is Level.YELLOW
    assert ReasonCode.STATE_CHANGE_SMALL in at.reasons


@pytest.mark.parametrize("cents", [999_00, _AT_LIMIT, 100_100])
def test_booking_is_state_change_and_ignores_amount_band(
    gateway: FactGateway, cents: int
) -> None:
    """`transfer.book` 登记不扣款：金额档不适用 ⇒ 三档恒黄（不得因金额大而静默升红）。"""
    outcome = _decide(gateway, "transfer.book", cents)
    assert outcome.amount_applies is False
    assert outcome.daily_accumulated_cents == 0
    assert outcome.level is Level.YELLOW
    assert outcome.reasons == (ReasonCode.STATE_CHANGE_SMALL,)


def test_booking_does_not_raise_daily_accumulation(
    gateway: FactGateway, core_bank: CoreBank
) -> None:
    """登记 900 元**不产生资金流**：随后评 200 元仍是 200 累计 ⇒ 黄，而不是 1100 ⇒ 红。"""
    core_bank.book_transfer(
        user_id=DEMO_USER_ID,
        account_id=_ACCOUNT,
        payee_id=_PAYEE,
        amount=Money(900_00),
        booked_on=DEFAULT_BOOKING_DATE,
        remark="",
        idempotency_key="book900",
    )

    outcome = _decide(gateway, "transfer.create", 200_00)
    assert outcome.daily_accumulated_cents == 200_00
    assert outcome.level is Level.YELLOW


def test_request_collection_is_red_regardless_of_amount(gateway: FactGateway) -> None:
    """官方点名红色：发起 AA 收款 100 元也必须红（不得因金额小而降为黄）。"""
    outcome = _decide(gateway, "transfer.request_collection", 100_00)
    assert outcome.level is Level.RED
    assert ReasonCode.OFFICIAL_RED in outcome.reasons
