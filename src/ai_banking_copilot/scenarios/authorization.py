"""节点级裁决请求构造器：评测链（07）与产品链（06）**唯一**的口径来源。

G3（S2 一致性收口）落点：此前 web `_object_state` / `_amounts` 与 eval `_object_state` /
`_node_amount` / `_node_daily` 各写一份对象态与金额口径，导致「同动作不同级别」。
本模块把两链收敛为同一组纯函数——**同节点 ⇒ 同请求 ⇒ 同裁决**（README 裁决 #23）。

口径（spec §3.2）：
- 对象态：**先找对手方**（`payee_id` = 转出对象；`payer_id` = **收款**请求的付款方，P2-3 裁决）；
  无对手方 ⇒ `SELF`；对手方 `blocked` ⇒ `BLOCKED`（吸收态）；
  `identity_confirmed` ⇒ `VERIFIED_SAVED`；否则 `KNOWN_UNVERIFIED`；
  查不到（`GatewayError`）⇒ `UNKNOWN`。
- 金额档仅对**流出型**动作（`ACTION_KINDS[action] is OUTFLOW`）适用：`amount_applies=True`
  时 `amount` 取 `spec.money_param`、`daily_accumulated` = 当日流出累计 + 本笔；
  非流出型一律 `amount=0`、`daily_accumulated=0`（金额档不适用，交回 PDP 的 `amount_applies`）。

本模块**纯函数、零 I/O**：所有事实取自传入的只读事实网关。
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from ai_banking_copilot.contracts.plan import PlanNode
from ai_banking_copilot.gateway.errors import GatewayError
from ai_banking_copilot.gateway.facts import Record
from ai_banking_copilot.gateway.models import Hold, Money, Payee, Subscription
from ai_banking_copilot.planning.llm import ACTION_KINDS, ActionKind
from ai_banking_copilot.policy.pdp import ObjectState, PolicyRequest
from ai_banking_copilot.tools.registry import ToolSpec

COUNTERPARTY_PARAMS: tuple[str, ...] = ("payee_id", "payer_id")
"""对手方参数名（**顺序即优先级**）：`payee_id` = 转出对象；`payer_id` = **收款**请求的付款方。

把 `payer_id` 纳入口径的理由（P2-3 裁决）：阻断名单若对「收款」不生效，等于给被冻结 / 涉诈的
对手方留了一条**资金入口**；收款请求的对手方就是付款方，必须与转出对象同口径处置。
**新增收款类动作时，对手方参数名必须落进本元组**，否则阻断名册对该动作形同虚设。
"""


def counterparty_id(node: PlanNode) -> str | None:
    """取节点的对手方主键（按 `COUNTERPARTY_PARAMS` 优先级）；无则 `None`。"""
    for name in COUNTERPARTY_PARAMS:
        raw = node.params.get(name)
        if isinstance(raw, str) and raw:
            return raw
    return None


def object_state_for_node(gateway: AuthorizationFacts, node: PlanNode) -> ObjectState:
    """由节点参数里的**对手方**推出对象态（谓词 = `identity_confirmed`；`blocked` 为吸收态）。"""
    raw = counterparty_id(node)
    if raw is None:
        return ObjectState.SELF
    try:
        payee = gateway.payee(raw).payload
    except GatewayError:
        return ObjectState.UNKNOWN
    if payee.blocked:
        return ObjectState.BLOCKED
    if payee.identity_confirmed:
        return ObjectState.VERIFIED_SAVED
    return ObjectState.KNOWN_UNVERIFIED


def node_policy_request(
    node: PlanNode,
    *,
    spec: ToolSpec,
    gateway: AuthorizationFacts,
    user_id: str,
    today: date,
    flags: frozenset[str],
) -> PolicyRequest:
    """把计划节点组装成单节点裁决请求（评测链与产品链的同一口径）。"""
    amount_applies = ACTION_KINDS.get(node.action) is ActionKind.OUTFLOW
    amount = _declared_amount(node, spec, amount_applies)
    baseline = gateway.daily_transfer_total(user_id, today).cents
    daily_accumulated = Money(baseline + amount.cents) if amount_applies else Money(0)
    return PolicyRequest(
        user_id=user_id,
        action=node.action,
        amount=amount,
        daily_accumulated=daily_accumulated,
        object_state=object_state_for_node(gateway, node),
        is_pure_query=spec.is_pure_query,
        irreversible=not spec.reversible,
        daily_limit=gateway.daily_transfer_limit(user_id),
        context_flags=flags,
        amount_applies=amount_applies,
    )


def _declared_amount(node: PlanNode, spec: ToolSpec, amount_applies: bool) -> Money:
    """金额仅在**流出型**动作下取 `spec.money_param`（缺失 / 非整数即 0）。

    非整数回退为 0 与原 web `_param_cents` / eval `_node_amount` 的判据一致（最小等价迁移）。
    """
    if not amount_applies or spec.money_param is None:
        return Money(0)
    raw = node.params.get(spec.money_param, 0)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return Money(0)
    return Money(raw)
class AuthorizationFacts(Protocol):
    def payee(self, payee_id: str) -> Record[Payee]: ...
    def subscription(self, subscription_id: str) -> Record[Subscription]: ...
    def hold(self, hold_id: str) -> Record[Hold]: ...
    def daily_transfer_total(self, user_id: str, on: date) -> Money: ...
    def daily_transfer_limit(self, user_id: str) -> Money: ...
