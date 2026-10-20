"""按服务端业务日期装配待办提案；执行仍须经过计划卡确认与 PEP。"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from ai_banking_copilot.contracts.plan import AnchorSource, IntentRef, Plan
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


class PendingHandler:
    """只装配用户请求检查的到期项目，不继承登记时的执行授权。"""

    name = "pending"

    def __init__(self, user_id: str, today: date) -> None:
        self.user_id = user_id
        self.today = today

    def matches(self, intents: Sequence[Intent]) -> bool:
        return False

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        scheduled = due_transfer_plan(gateway, self.today, user_id=self.user_id)
        birthday = birthday_plan(gateway, self.today, user_id=self.user_id)
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
        return Plan(plan_id="pending", nodes=nodes)

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
        for node in plan.nodes:
            refs: list[str] = []
            missing: list[str] = []
            basis: dict[str, object]
            if node.action == "transfer.create":
                record = schedules.get(str(node.params.get("schedule_id")))
                basis = {
                    "source": "scheduled_transfer",
                    "rule": "本人预约仍为 scheduled 且 booked_on <= request.today 才可提案",
                    "parameter_sources": {
                        key: "scheduled_transfer" for key in (
                            "schedule_id", "account_id", "payee_id", "amount_cents", "booked_on",
                        )
                    } | {"executed_on": "request.today"},
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
            elif node.action == "merchant.order":
                basis = {
                    "source": "server_rule", "rule": "birthday_order",
                    "date_rule": "本年生日减 lead_days，order_on <= request.today 且未完成才可提案",
                }
                profile = gateway.user_profile(self.user_id)
                birthday = (
                    cross._birthday_in_year(profile.payload.birthday, self.today.year)
                    if profile is not None else None
                )
                if profile is None or birthday is None:
                    missing.append("birthday_profile")
                else:
                    refs.extend((profile.record_id, "demo_configuration:birthday_order"))
                    facts.append({
                        "source": "gateway", "record_id": profile.record_id,
                        "content_hash": profile.content_hash, "user_id": profile.payload.user_id,
                        "birthday": profile.payload.birthday,
                    })
                    configuration: dict[str, object] = {
                        "source": "demo_configuration",
                        "record_id": "demo_configuration:birthday_order",
                        "merchant": BIRTHDAY_MERCHANT,
                        "amount_cents": BIRTHDAY_ORDER_CENTS, "unit": "cents",
                        "lead_days": cross._BIRTHDAY_LEAD_DAYS,
                    }
                    # 配置摘要不是账本凭证，source 保留 demo_configuration。
                    configuration["content_hash"] = hashlib.sha256(
                        json.dumps(
                            configuration, ensure_ascii=False, sort_keys=True,
                        ).encode("utf-8"),
                    ).hexdigest()
                    facts.append(configuration)
                    occasion_id = f"birthday:{self.today.year}:flowers"
                    basis.update({
                        "birthday_on": birthday.isoformat(),
                        "order_on": due_date(
                            birthday, lead_days=cross._BIRTHDAY_LEAD_DAYS,
                        ).isoformat(),
                        "occasion_id": occasion_id,
                        "occasion_completed": gateway.merchant_occasion_completed(
                            self.user_id, occasion_id,
                        ),
                    })
            else:
                basis = {}
                missing.append("due_proposal_rule")
            contexts[node.node_id] = {
                "fact_refs": refs, "proposal_basis": basis, "missing_sources": missing,
            }
        return facts, contexts
