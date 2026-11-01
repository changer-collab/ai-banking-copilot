"""飞书按钮按级别分流：YELLOW 可确认，RED / 缺数据只给链接。"""

from __future__ import annotations

from ai_banking_copilot.web.channels.contracts import CHANNEL_FEISHU, OutboundMessage
from ai_banking_copilot.web.channels.feishu import FeishuAdapter, FeishuConfig


def _outbound(level: str | None) -> OutboundMessage:
    return OutboundMessage(
        channel=CHANNEL_FEISHU,
        chat_id="oc_x",
        session_id="feishu:oc_x",
        state="awaiting_confirm",
        text="请确认",
        card={"card_id": "c1", "frozen_hash": "h1"},
        level=level,
    )


def _buttons(outbound: OutboundMessage) -> list[dict[str, object]]:
    adapter = FeishuAdapter(FeishuConfig(), clock=lambda: 0.0)
    elements = adapter.render(outbound)["elements"]
    assert isinstance(elements, list)
    actions = [item for item in elements if item.get("tag") == "action"]
    assert actions
    buttons = actions[0]["actions"]
    assert isinstance(buttons, list)
    return buttons


def test_yellow_card_has_confirm_button() -> None:
    buttons = _buttons(_outbound("YELLOW"))
    assert all(button.get("value", {}).get("action") == "confirm" for button in buttons)


def test_red_card_only_has_url_button() -> None:
    buttons = _buttons(_outbound("RED"))
    assert all(button.get("url") for button in buttons)
    assert all("value" not in button for button in buttons)


def test_missing_level_card_only_has_url_button() -> None:
    buttons = _buttons(_outbound(None))
    assert all(button.get("url") for button in buttons)
    assert all("value" not in button for button in buttons)
