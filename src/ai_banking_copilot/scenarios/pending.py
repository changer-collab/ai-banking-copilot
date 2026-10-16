"""按服务端业务日期装配待办提案；执行仍须经过计划卡确认与 PEP。"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from ai_banking_copilot.contracts.plan import AnchorSource, IntentRef, Plan
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.cross import birthday_plan
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
                    source=AnchorSource.USER_APPROVED,
                    anchor_id=ledger.grant_approved("到期待办提案：须在计划卡上批准后执行"),
                ),
            )
            for node in (*scheduled, *birthday)
        )
        return Plan(plan_id="pending", nodes=nodes)
