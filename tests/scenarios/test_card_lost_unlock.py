"""挂失不可逆：已挂失的卡不允许在对话通道被「解锁」回 active。

口径来源：
- 04 三色基准表把 `card.report_lost` 列为不可逆（PDP 的 `irreversible` 修饰）；
- 手动实测清单 CRD-06 要求「挂失后不能再解锁」；
- 卡片面板本来就不给 lost 卡显示解锁按钮，此前只有对话通道能把它改回 active，
  属面板与后端能力不一致（10-29 自测发现）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import ScenarioRefused
from ai_banking_copilot.scenarios.cards import CardsHandler


def _gateway(tmp_path: Path, *, lost: str) -> FactGateway:
    """建库播种后把指定卡置为 lost（只改这一行的状态）。"""
    db_path = tmp_path / "lost.db"
    writer = Store(db_path)
    init_schema(writer)
    seed_database(writer)
    with writer.transaction() as conn:
        conn.execute("UPDATE cards SET status = 'lost' WHERE card_id = ?", (lost,))
    writer.close()
    return FactGateway(Store(db_path, StoreMode.READ_ONLY))


def _intent(action: str, card_id: str) -> Intent:
    return Intent(
        action=action,
        slots={"card": Value(card_id, Trust.TRUSTED)},
        confidence=0.9,
        evidence=action,
    )


def test_unlock_of_a_lost_card_is_refused(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, lost="card_debit")
    with pytest.raises(ScenarioRefused) as error:
        CardsHandler().build([_intent("card.unlock", "card_debit")], gateway)
    assert "挂失是不可逆操作" in str(error.value)


def test_unlock_of_a_healthy_card_still_builds(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, lost="card_debit")
    plan = CardsHandler().build([_intent("card.unlock", "card_credit")], gateway)
    assert plan.nodes[0].action == "card.unlock"
    assert plan.nodes[0].params["card_id"] == "card_credit"


def test_lost_card_can_still_be_replaced(tmp_path: Path) -> None:
    """挂失后仍可补卡：拒绝的只是「回到 active」，不是把卡彻底锁死。"""
    gateway = _gateway(tmp_path, lost="card_debit")
    intent = Intent(
        action="card.apply_virtual",
        slots={"daily_limit": Value(Money(50000), Trust.TRUSTED)},
        confidence=0.9,
        evidence="补卡",
    )
    plan = CardsHandler().build([intent], gateway)
    assert plan.nodes[0].action == "card.apply_virtual"
