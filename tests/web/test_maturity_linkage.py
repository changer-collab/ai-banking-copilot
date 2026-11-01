"""理财到期联动（Q17）：到期日推算只做「起息日 + 产品期限」的机械加法。"""

from __future__ import annotations

from datetime import date

from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.scenarios.life_events import (
    build_maturity_link,
    detect_maturity_link,
    holding_maturity,
)
from ai_banking_copilot.web.app import _life_event_name, _life_maturities


def test_holding_maturity_adds_term_days() -> None:
    assert holding_maturity(date(2026, 10, 1), 30) == date(2026, 10, 31)


def test_holding_maturity_rejects_non_positive_term() -> None:
    assert holding_maturity(date(2026, 10, 1), 0) is None


def test_maturity_link_is_recorded_as_a_life_event() -> None:
    """到期联动也要进记录页：它有显示名，但不进七条原型（L1–L7）的对外口径。"""
    assert _life_event_name("maturity") == "理财到期处理"
    assert detect_maturity_link("理财到期了帮我订一束花") is True
    assert detect_maturity_link("查一下我的理财持仓") is False


def test_life_maturities_mark_expired_and_upcoming(gateway: FactGateway) -> None:
    """状态只是把推算到期日与今天比较：已过期 `expired`、未到期 `upcoming`，负数天数如实给出。"""
    rows = {
        str(row["product_id"]): row
        for row in _life_maturities(gateway, DEMO_USER_ID, DEFAULT_BOOKING_DATE)
    }
    # 活期推不出到期日：不列出、不补 0
    assert "wp_cash" not in rows

    bond = rows["wp_bond"]
    assert bond["status"] == "expired"
    assert isinstance(bond["days_left"], int) and bond["days_left"] < 0

    short = rows["wp_short"]
    assert short["status"] == "upcoming"
    assert isinstance(short["days_left"], int) and short["days_left"] == 30


def test_maturity_link_picks_the_upcoming_holding(gateway: FactGateway) -> None:
    """无显式产品且有多笔候选：确定性取「最近要到期的那一笔」（未到期优先）。"""
    plan = build_maturity_link({}, gateway)
    redeem = next(node for node in plan.nodes if node.action == "wealth.redeem")
    assert redeem.params["product_id"] == "wp_short"
