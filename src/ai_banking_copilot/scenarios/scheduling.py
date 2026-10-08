"""公共日期调度：把「按基准日派发到期计划」抽成**一份**机制（`docs/待修复清单.md` 步骤 3）。

两处消费（此前各写一套的隐患由此消除）：
- **P2-6** 定时转账到期派发（`transfer.schedule` 的 `booked_on` 到点执行）；
- **P2-5** 生日联动（`生日 − lead_days` 到点下单）。

分层纪律：
- **单一基准时钟**：基准日只有 `gateway.core_bank.DEFAULT_BOOKING_DATE` 一个来源，本模块只转发；
- **只做日期算术与「哪些到期」的筛选**，不改事实（H1）、不裁决级别（级别归 PDP）、不执行；
- 纯函数、无 I/O、无随机：同一输入恒得同一输出（评测可复现）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from ai_banking_copilot.contracts.plan import AnchorSource, IntentRef, PlanNode
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.scenarios.base import grant_anchor

REFERENCE_DATE: date = DEFAULT_BOOKING_DATE
"""演示与评测的**统一基准日**（转发 01 的 `DEFAULT_BOOKING_DATE`；全项目只此一个时钟）。"""


@dataclass(frozen=True)
class ScheduledItem:
    """一条待派发的到期项：`key` 唯一标识、`due_on` 到期日、`action` + `params` 即待建节点。"""

    key: str
    due_on: date
    action: str
    params: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("ScheduledItem.key 不能为空")
        if not self.action:
            raise ValueError("ScheduledItem.action 不能为空")


def due_date(base: date, *, lead_days: int = 0) -> date:
    """到期日 = 基准日 − 提前天数（「生日前 2 天」⇒ `lead_days=2`）。"""
    if lead_days < 0:
        raise ValueError("lead_days 不能为负（提前量只能是 0 或正数）")
    return base - timedelta(days=lead_days)


def is_due(target: date, *, as_of: date) -> bool:
    """`target` 是否在基准日**当日或之前**（含当日）到期。"""
    return target <= as_of


def dispatch_due(items: Sequence[ScheduledItem], *, as_of: date) -> tuple[ScheduledItem, ...]:
    """按到期日升序返回**已到期**项；同一到期日按 `key` 排序（确定性、可复现）。"""
    due = [item for item in items if is_due(item.due_on, as_of=as_of)]
    return tuple(sorted(due, key=lambda item: (item.due_on, item.key)))


def plan_due(
    items: Sequence[ScheduledItem],
    *,
    as_of: date,
    ledger: AnchorLedger,
    node_prefix: str = "due",
    source: AnchorSource = AnchorSource.EXPLICIT,
) -> tuple[PlanNode, ...]:
    """把已到期项装配成带锚点的计划节点（编号 1..n，顺序即到期序）。

    只装配**节点**；是否落成一个 `Plan`、以及该计划需要什么前置（如冻结预算），仍由各场景
    处理器自行决定——本模块不跨域替别人做决定。
    """
    nodes: list[PlanNode] = []
    for index, item in enumerate(dispatch_due(items, as_of=as_of), start=1):
        anchor_id = grant_anchor(ledger, source, f"到期派发：{item.key}")
        nodes.append(
            PlanNode(
                node_id=f"{node_prefix}{index}",
                action=item.action,
                params=dict(item.params),
                intent=IntentRef(source=source, anchor_id=anchor_id),
            )
        )
    return tuple(nodes)
