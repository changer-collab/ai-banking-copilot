"""卡片槽选卡逻辑：「都锁卡」说明边界、卡类型词唯一命中自动绑定、同类型多张仍追问。

背景（`docs/2026-10-28/验收记录/林晓研.md` §5 第 5 条）：说「把可疑的都锁卡」时原先只回
「请选择要处理的卡片」，像是在等用户挑一张——既不说明做不到批量，也不说明凭什么是这张。
本文件锁住三条口径：

1. **全量语义不挑一张顶替**：明确说「一次只能一张 + 流水未记录卡号，无法反推」；
2. **卡类型词唯一命中就直接绑定**：「锁信用卡」不再白问一轮；
3. **同类型多张仍追问**：不按 id / 顺序取第一张。
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from ai_banking_copilot.gateway.models import Card, Money
from ai_banking_copilot.planning.slots import (
    CARD_BULK_PROMPT,
    SlotInputs,
    fill_control_slots,
)
from ai_banking_copilot.web.app import create_app

TODAY = date(2026, 10, 28)


def _card(card_id: str, card_type: str) -> Card:
    return Card(
        card_id=card_id,
        user_id="u_probe",
        card_type=card_type,
        status="active",
        daily_limit=Money(2_000_000),
    )


_CARDS = (_card("card_debit", "debit"), _card("card_credit", "credit"))


def _fill(text: str, cards: tuple[Card, ...] = _CARDS):  # type: ignore[no-untyped-def]
    return fill_control_slots("card.lock", SlotInputs(text=text, today=TODAY, cards=cards))


def test_bulk_lock_request_explains_the_boundary_instead_of_picking_a_card() -> None:
    fill = _fill("把可疑的都锁卡")
    assert "card" not in fill.slots
    assert tuple(question.prompt for question in fill.questions) == (CARD_BULK_PROMPT,)
    assert "card:bulk request unsupported" in fill.trace


def test_credit_card_word_binds_the_only_credit_card() -> None:
    fill = _fill("锁信用卡")
    assert fill.complete
    assert fill.slots["card"] == "card_credit"
    assert "resolved card:card_credit" in fill.trace


def test_debit_card_word_binds_the_only_debit_card() -> None:
    fill = _fill("锁定储蓄卡")
    assert fill.slots["card"] == "card_debit"


def test_two_cards_of_the_same_kind_still_ask() -> None:
    cards = (_card("card_credit_a", "credit"), _card("card_credit_b", "credit"))
    fill = _fill("锁信用卡", cards)
    assert "card" not in fill.slots
    assert tuple(question.prompt for question in fill.questions) == (
        "请选择要处理的卡片（一次一张）。",
    )


def test_bare_lock_request_still_asks_which_card() -> None:
    fill = _fill("锁卡")
    assert "card" not in fill.slots
    assert tuple(question.prompt for question in fill.questions) == (
        "请选择要处理的卡片（一次一张）。",
    )


def test_channel_binds_the_credit_card_from_the_utterance(deps) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(create_app(deps))
    response = client.post(
        "/api/submit", json={"utterance": "锁信用卡", "session_id": "card-slot-e2e"}
    )
    data = response.json()
    assert data["state"] == "awaiting_confirm"
    assert data["card"]["level"] == "YELLOW"
    # 计划卡显示卡名与尾号，不回显内部 ID（CLR-03）。
    line = data["card"]["lines"][0]
    assert "招商银行信用卡" in line
    assert "尾号 9021" in line
    assert "card_credit" not in line


def test_channel_answers_the_bulk_request_with_the_boundary(deps) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(create_app(deps))
    response = client.post(
        "/api/submit", json={"utterance": "把可疑的都锁卡", "session_id": "card-slot-bulk"}
    )
    data = response.json()
    assert data["state"] == "needs_clarification"
    assert data["card"] is None
    assert data["questions"] == [CARD_BULK_PROMPT]
