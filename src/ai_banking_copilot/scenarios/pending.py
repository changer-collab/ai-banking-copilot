"""按服务端业务日期装配待办提案；执行仍须经过计划卡确认与 PEP。"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date

from ai_banking_copilot.contracts.plan import AnchorSource, IntentRef, Plan, PlanNode
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios import cross
from ai_banking_copilot.scenarios.cross import (
    BIRTHDAY_MERCHANT,
    BIRTHDAY_ORDER_CENTS,
    birthday_plan,
)
from ai_banking_copilot.scenarios.scheduling import due_date
from ai_banking_copilot.scenarios.transfer import due_transfer_plan
from ai_banking_copilot.web.present import yuan

_BIRTHDAY_ACTIONS = frozenset({"funds.freeze", "merchant.order", "funds.release"})
"""生日预算链的三个动作：冻结 → 下单 → 解冻，共用同一份生日依据。"""

_LIFE_NODE_PATTERN = re.compile(r"^life\d+-")
"""到期预约节点在计划里的 ID 前缀（`life0-n1`）：**唯一**能识别来源的字段。

预约 ID 不能写进 `PlanNode.params`：`policy.pep` 的节点成员资格比对拿计划节点参数与
调用参数逐字比较，多一个键就会被判「不属于已确认计划」。故来源只由节点 ID 承载。
"""


@dataclass(frozen=True)
class LifeDueSchedule:
    """到期的生活事件预约：确认时冻结的节点快照由会话层读出，本层只重建计划节点。"""

    schedule_id: str
    event_id: str
    event_title: str
    due_on: date
    nodes: tuple[Mapping[str, object], ...]
    kind: str = "order"
    """预约类型：`reminder` = 到提醒日先提案，`order` = 到期直接下单。"""
    notify_on: date | None = None
    """提醒日；`None` = 未单独设置提醒。"""


def _schedule_param(schedule: LifeDueSchedule, action: str, key: str) -> object | None:
    """从登记时的节点快照里取某一动作的参数；找不到返回 `None`。"""
    for item in schedule.nodes:
        if str(item.get("action") or "") != action:
            continue
        params = item.get("params")
        if not isinstance(params, Mapping):
            return None
        return params.get(key)
    return None


def _schedule_text(raw: object) -> str | None:
    """快照参数 → 展示用文本；读不出返回 `None`（宁可不显示也不猜）。"""
    if isinstance(raw, date):
        return raw.isoformat()
    return raw if isinstance(raw, str) and raw else None


def _node_evidence_hash(node: PlanNode) -> str:
    """节点参数摘要：证明该节点来自登记时的快照，而不是本次请求临时拼的。"""
    return hashlib.sha256(
        json.dumps(node.params, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"),
    ).hexdigest()


def _birthday_evidence(
    gateway: FactGateway, user_id: str, today: date,
) -> tuple[list[str], list[dict[str, object]], dict[str, object], list[str]]:
    """生日依据（三条节点共用）：本人档案 + 演示配置摘要 + 到期日算术。

    配置摘要不是账本凭证，`source` 保留 `demo_configuration`；金额 / 提前天数只从此处取，
    不从计划参数复制（H1：计划参数不能为自己提供证据）。
    """
    profile = gateway.user_profile(user_id)
    birthday = (
        cross._birthday_in_year(profile.payload.birthday, today.year)
        if profile is not None else None
    )
    if profile is None or birthday is None:
        return [], [], {}, ["birthday_profile"]
    order_on = due_date(birthday, lead_days=cross._BIRTHDAY_LEAD_DAYS)
    occasion_id = f"birthday:{today.year}:flowers"
    configuration: dict[str, object] = {
        "source": "demo_configuration",
        "record_id": "demo_configuration:birthday_order",
        "merchant": BIRTHDAY_MERCHANT,
        "amount_cents": BIRTHDAY_ORDER_CENTS, "unit": "cents",
        "budget_cents": cross.BIRTHDAY_BUDGET_CENTS,
        "lead_days": cross._BIRTHDAY_LEAD_DAYS,
    }
    configuration["content_hash"] = hashlib.sha256(
        json.dumps(configuration, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    ).hexdigest()
    facts: list[dict[str, object]] = [
        {
            "source": "gateway", "record_id": profile.record_id,
            "content_hash": profile.content_hash, "user_id": profile.payload.user_id,
            "birthday": profile.payload.birthday,
        },
        configuration,
    ]
    basis: dict[str, object] = {
        "source": "server_rule",
        "date_rule": "本年生日减 lead_days，order_on <= request.today 且未完成才可提案",
        "birthday_on": birthday.isoformat(),
        "order_on": order_on.isoformat(),
        "occasion_id": occasion_id,
        "occasion_completed": gateway.merchant_occasion_completed(user_id, occasion_id),
    }
    return [profile.record_id, "demo_configuration:birthday_order"], facts, basis, []


class PendingHandler:
    """只装配用户请求检查的到期项目，不继承登记时的执行授权。"""

    name = "pending"

    def __init__(
        self, user_id: str, today: date, *, life_schedules: Sequence[LifeDueSchedule] = (),
    ) -> None:
        self.user_id = user_id
        self.today = today
        self.life_schedules = life_schedules

    def matches(self, intents: Sequence[Intent]) -> bool:
        return False

    def reminder_notice(self) -> str | None:
        """提醒类预约的提案文案；取不到下单日 / 预算就返回 `None`（退回模板文案）。"""
        for schedule in self.life_schedules:
            if schedule.kind != "reminder":
                continue
            order_on = _schedule_text(_schedule_param(schedule, "merchant.order", "order_on"))
            budget_yuan = yuan(_schedule_param(schedule, "funds.freeze", "amount_cents"))
            if order_on is None or not budget_yuan:
                continue
            return (
                f"提醒：{(schedule.notify_on or schedule.due_on).isoformat()} 起可下单，"
                f"{order_on} 送达；预算 {budget_yuan} 元。确认后执行，今天不扣款。"
            )
        return None

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        scheduled = due_transfer_plan(gateway, self.today, user_id=self.user_id)
        # 用户已登记过同一场合的预算链（冻结 / 下单 / 解冻），就不再叠加档案派生的生日链：
        # 两条链会给同一件事各下一次单，属于重复扣款。
        birthday = (
            () if _BIRTHDAY_ACTIONS <= self._scheduled_actions()
            else birthday_plan(gateway, self.today, user_id=self.user_id)
        )
        ledger = AnchorLedger()
        nodes = tuple(
            replace(
                node,
                intent=IntentRef(
                    source=AnchorSource.EXPLICIT,
                    anchor_id=ledger.grant_explicit("用户请求检查到期待办；执行仍须确认计划卡"),
                ),
            )
            for node in (*scheduled, *birthday)
        )
        return Plan(plan_id="pending", nodes=(*nodes, *self._life_nodes(ledger)))

    def _life_nodes(self, ledger: AnchorLedger) -> tuple[PlanNode, ...]:
        """把到期预约的节点快照重建成新计划；节点 ID 加前缀避免与会话内其他节点撞名。"""
        rebuilt: list[PlanNode] = []
        for index, schedule in enumerate(self.life_schedules):
            prefix = f"life{index}-"
            identifiers = {
                str(item.get("node_id") or ""): f"{prefix}{item.get('node_id') or ''}"
                for item in schedule.nodes
            }
            for item in schedule.nodes:
                params = item.get("params")
                depends = item.get("depends_on")
                rebuilt.append(
                    PlanNode(
                        node_id=identifiers[str(item.get("node_id") or "")],
                        action=str(item.get("action") or ""),
                        params=dict(params) if isinstance(params, Mapping) else {},
                        intent=IntentRef(
                            source=AnchorSource.EXPLICIT,
                            anchor_id=ledger.grant_explicit(
                                f"本人到期预约 {schedule.schedule_id}"
                                f"（{schedule.due_on.isoformat()}）；执行仍须确认计划卡"
                            ),
                        ),
                        depends_on=tuple(
                            identifiers.get(str(dep), str(dep))
                            for dep in depends
                        ) if isinstance(depends, Sequence) and not isinstance(depends, str) else (),
                    ),
                )
        return tuple(rebuilt)

    def life_node_ids(self, plan: Plan) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """`(预约 ID, 该预约在计划里的节点 ID)`：结算只看这条预约自己的节点成败。"""
        grouped: list[tuple[str, tuple[str, ...]]] = []
        for index, schedule in enumerate(self.life_schedules):
            prefix = f"life{index}-"
            node_ids = tuple(
                node.node_id for node in plan.nodes if node.node_id.startswith(prefix)
            )
            if node_ids:
                grouped.append((schedule.schedule_id, node_ids))
        return tuple(grouped)

    def _scheduled_actions(self) -> frozenset[str]:
        """到期预约快照里出现过的动作集合。"""
        return frozenset(
            str(item.get("action") or "")
            for schedule in self.life_schedules
            for item in schedule.nodes
        )

    def _life_schedule(self, node: PlanNode) -> LifeDueSchedule | None:
        """按节点 ID 前缀找回登记快照；找不到即视为依据缺失（fail-closed）。"""
        if _LIFE_NODE_PATTERN.match(node.node_id) is None:
            return None
        for index, schedule in enumerate(self.life_schedules):
            prefix = f"life{index}-"
            if not node.node_id.startswith(prefix):
                continue
            if any(
                f"{prefix}{item.get('node_id') or ''}" == node.node_id
                for item in schedule.nodes
            ):
                return schedule
        return None

    def review_context(
        self, gateway: FactGateway, plan: Plan,
    ) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
        """按节点回读登记和生日依据；不把计划参数复制成事实或继承旧授权。"""
        facts: list[dict[str, object]] = []
        contexts: dict[str, dict[str, object]] = {}
        schedules = {
            record.payload.schedule_id: record
            for record in gateway.scheduled_transfers_of(self.user_id)
        }
        budget_refs, budget_facts, budget_basis, budget_missing = _birthday_evidence(
            gateway, self.user_id, self.today,
        )
        budget_facts_added = False
        for node in plan.nodes:
            refs: list[str] = []
            missing: list[str] = []
            basis: dict[str, object]
            life_schedule = self._life_schedule(node)
            if life_schedule is not None:
                # 预约节点必须先于生日链判断：两者动作相同，只能靠来源区分。
                basis = {
                    "source": "life_event_schedule",
                    "rule": "本人到期预约仍为 scheduled 且 due_on <= request.today 才可提案",
                    "event_id": life_schedule.event_id,
                    "event_title": life_schedule.event_title,
                    "due_on": life_schedule.due_on.isoformat(),
                }
                facts.append({
                    "source": "life_event_schedule",
                    "record_id": f"life_event_schedule:{life_schedule.schedule_id}",
                    "content_hash": _node_evidence_hash(node),
                    "schedule_id": life_schedule.schedule_id,
                    "user_id": self.user_id,
                    "event_id": life_schedule.event_id,
                    "due_on": life_schedule.due_on.isoformat(),
                })
                refs.append(f"life_event_schedule:{life_schedule.schedule_id}")
            elif _LIFE_NODE_PATTERN.match(node.node_id) is not None:
                basis = {"source": "life_event_schedule"}
                missing.append("life_event_schedule")
            elif node.action == "transfer.create":
                record = schedules.get(str(node.params.get("schedule_id")))
                basis = {
                    "source": "scheduled_transfer",
                    "rule": "本人预约仍为 scheduled 且 booked_on <= request.today 才可提案",
                    "parameter_sources": {
                        key: "scheduled_transfer" for key in (
                            "schedule_id", "account_id", "payee_id", "amount_cents", "booked_on",
                        )
                    } | {"executed_on": "request.today", "currency": "account_currency"},
                }
                if record is None:
                    missing.append("scheduled_transfer")
                else:
                    item = record.payload
                    refs.append(record.record_id)
                    facts.append({
                        "source": "gateway", "record_id": record.record_id,
                        "content_hash": record.content_hash, "schedule_id": item.schedule_id,
                        "user_id": item.user_id, "account_id": item.account_id,
                        "payee_id": item.payee_id, "amount_cents": item.amount.cents,
                        "booked_on": item.booked_on.isoformat(), "status": item.status,
                    })
            elif node.action in _BIRTHDAY_ACTIONS:
                rule = "birthday_order" if node.action == "merchant.order" else "birthday_budget"
                basis = {**budget_basis, "rule": rule}
                refs.extend(budget_refs)
                missing.extend(budget_missing)
                if not budget_facts_added:
                    facts.extend(budget_facts)
                    budget_facts_added = True
            else:
                basis = {}
                missing.append("due_proposal_rule")
            contexts[node.node_id] = {
                "fact_refs": refs, "proposal_basis": basis, "missing_sources": missing,
            }
        return facts, contexts
