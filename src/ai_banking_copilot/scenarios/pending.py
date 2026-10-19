"""按服务端业务日期装配待办提案；执行仍须经过计划卡确认与 PEP。"""

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

    def review_sources(self, gateway: FactGateway) -> list[dict[str, object]]:
        """独立回读真正到期的登记和生日资料；配置常量明示来源。"""
        facts: list[dict[str, object]] = []
        for record in gateway.scheduled_transfers_of(self.user_id, status="scheduled"):
            item = record.payload
            if item.booked_on > self.today:
                continue
            facts.append({
                "source": "gateway", "record_id": record.record_id,
                "content_hash": record.content_hash, "schedule_id": item.schedule_id,
                "user_id": item.user_id, "account_id": item.account_id,
                "payee_id": item.payee_id, "amount_cents": item.amount.cents,
                "booked_on": item.booked_on.isoformat(), "status": item.status,
            })
        if birthday_plan(gateway, self.today, user_id=self.user_id):
            profile = gateway.user_profile(self.user_id)
            if profile is not None:
                facts.append({
                    "source": "gateway", "record_id": profile.record_id,
                    "content_hash": profile.content_hash, "user_id": profile.payload.user_id,
                    "birthday": profile.payload.birthday,
                })
                facts.append({
                    "source": "demo_configuration", "merchant": BIRTHDAY_MERCHANT,
                    "amount_cents": BIRTHDAY_ORDER_CENTS, "unit": "cents",
                    "lead_days": cross._BIRTHDAY_LEAD_DAYS,
                })
        return facts
