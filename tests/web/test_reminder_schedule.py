"""Q16：『前 2 天提醒』登记在提醒日，下单日仍是生日当天。"""

from __future__ import annotations

from ai_banking_copilot.web.session import _l1_slot_reminder


def test_reminder_lead_is_parsed_from_text() -> None:
    assert _l1_slot_reminder("我爱人生日前2天提醒我订蛋糕和鲜花，预算500") == 2
    assert _l1_slot_reminder("提前 3 天提醒我") == 3
    assert _l1_slot_reminder("我下周三生日，帮我订一束花") is None
