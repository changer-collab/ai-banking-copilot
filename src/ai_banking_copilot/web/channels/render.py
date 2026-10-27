"""渠道渲染：**同一份 `OutboundMessage` 出三种形态**，不新增任何事实。

- `render_text`：本地 IM / 纯文本回执（计划卡逐行展开）；
- `render_web_html`：Web 气泡碎片（转义后输出，禁止注入）；
- `render_feishu_card`：飞书交互卡片（`header.template` 由三色等级映射）。

三端看到的 `state` / `level` / `lines` 全部来自服务端计划卡，**渲染层不判级、不改金额**。
"""

from __future__ import annotations

from collections.abc import Mapping
from html import escape

from ai_banking_copilot.web.channels.contracts import (
    CHANNEL_FEISHU,
    CHANNEL_LOCAL,
    OutboundMessage,
)

LEVEL_TITLES: Mapping[str, str] = {
    "GREEN": "绿色 · 可直接执行",
    "YELLOW": "黄色 · 需确认",
    "RED": "红色 · 强验证 + 人工复核",
}
"""等级 → 卡片标题（**只做展示**，等级事实来自 `card.level`）。"""

_FEISHU_TEMPLATES: Mapping[str, str] = {
    "GREEN": "green",
    "YELLOW": "orange",
    "RED": "red",
}
_DEFAULT_TEMPLATE = "blue"
_DEFAULT_TITLE = "AI 银行副驾"


def card_lines(outbound: OutboundMessage) -> list[str]:
    """计划卡逐行文案（`card.lines` 不存在或类型不符时返回空列表）。"""
    card = outbound.card
    if not isinstance(card, Mapping):
        return []
    raw = card.get("lines")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item) for item in raw]


def card_requires(outbound: OutboundMessage) -> list[str]:
    """计划卡的前置条件（强验证 / 人工复核等）。"""
    card = outbound.card
    if not isinstance(card, Mapping):
        return []
    raw = card.get("requires")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item) for item in raw]


def render_text(outbound: OutboundMessage) -> str:
    """纯文本形态：会话层文案 + 计划卡逐行 + 前置条件。"""
    parts: list[str] = [outbound.text]
    lines = card_lines(outbound)
    if lines:
        parts.append("【计划卡】\n" + "\n".join(f"- {line}" for line in lines))
    requires = card_requires(outbound)
    if requires:
        parts.append("需要：" + "、".join(requires))
    return "\n\n".join(part for part in parts if part)


def render_web_html(outbound: OutboundMessage) -> str:
    """Web 气泡碎片：全部字段先转义，计划卡以列表渲染。"""
    lines = card_lines(outbound)
    items = ""
    if lines:
        rows = "".join(f"<li>{escape(line)}</li>" for line in lines)
        items = f'<ul class="channel-card-lines">{rows}</ul>'
    requires = card_requires(outbound)
    footer = ""
    if requires:
        footer = (
            '<p class="channel-card-requires">'
            + escape("需要：" + "、".join(requires))
            + "</p>"
        )
    return (
        f'<section class="channel-reply" data-channel="{escape(outbound.channel)}"'
        f' data-kind="{escape(outbound.kind)}" data-state="{escape(outbound.state)}"'
        f' data-level="{escape(outbound.level or "")}">'
        f'<p class="channel-reply-text">{escape(outbound.text)}</p>'
        f"{items}{footer}</section>"
    )


def render_feishu_card(outbound: OutboundMessage) -> dict[str, object]:
    """飞书交互卡片：`header.template` 由等级映射，正文与计划卡同源。"""
    elements: list[dict[str, object]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": outbound.text}},
    ]
    lines = card_lines(outbound)
    if lines:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(f"- {line}" for line in lines),
                },
            }
        )
    requires = card_requires(outbound)
    if requires:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "需要：" + "、".join(requires)},
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": _FEISHU_TEMPLATES.get(outbound.level or "", _DEFAULT_TEMPLATE),
            "title": {
                "tag": "plain_text",
                "content": LEVEL_TITLES.get(outbound.level or "", _DEFAULT_TITLE),
            },
        },
        "elements": elements,
    }


def render_for(channel: str, outbound: OutboundMessage) -> Mapping[str, object] | str:
    """按渠道给出平台形态：本地 IM 出纯文本，飞书出交互卡片。"""
    if channel == CHANNEL_FEISHU:
        return render_feishu_card(outbound)
    if channel == CHANNEL_LOCAL:
        return render_text(outbound)
    raise ValueError(f"unsupported_channel:{channel}")
