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
    KIND_RECEIPT,
    OutboundMessage,
)

LEVEL_TITLES: Mapping[str, str] = {
    "GREEN": "绿色 · 可直接执行",
    "YELLOW": "黄色 · 需确认",
    "RED": "红色 · 强验证 + 人工复核",
}
"""等级 → 卡片标题（**只做展示**，等级事实来自 `card.level`）。"""

RECEIPT_TITLES: Mapping[str, str] = {
    "completed": "办理回执 · 执行完成",
    "partial": "办理回执 · 部分完成",
    "failed": "办理回执 · 执行失败",
    "blocked": "办理回执 · 未执行",
    "taken_over": "办理回执 · 转人工复核",
}
"""执行回执标题：按**执行结果**出标题，不再沿用计划卡的「需确认」。"""

_RECEIPT_DEFAULT_TITLE = "办理回执"

_FEISHU_TEMPLATES: Mapping[str, str] = {
    "GREEN": "green",
    "YELLOW": "orange",
    "RED": "red",
}
_RECEIPT_TEMPLATES: Mapping[str, str] = {
    "completed": "green",
    "partial": "orange",
    "failed": "red",
    "blocked": "red",
    "taken_over": "orange",
}
_DEFAULT_TEMPLATE = "blue"
_DEFAULT_TITLE = "AI 银行副驾"

_EXTERNAL_CANCEL_LABELS: Mapping[str, str] = {
    "not_integrated": "商户取消接口尚未接入（商户未确认）",
    "confirmed_by_merchant": "商户已确认取消",
}
"""订阅取消的**商户侧状态**中文口径；未知值原样展示，不臆造「已取消」。"""


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


def card_subscription_cancel(outbound: OutboundMessage) -> Mapping[str, object] | None:
    """取消订阅的**操作状态事实**（来自执行回执 payload，不在此处判级或改写）。"""
    payload = outbound.payload
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("subscription_cancel")
    return raw if isinstance(raw, Mapping) else None


def _subscription_cancel_text(cancel: Mapping[str, object]) -> str:
    """本地状态 / 商户侧状态 / 能力边界：三者必须一起出现，避免被读成「商户已停止扣费」。"""
    label = str(cancel.get("label") or "")
    boundary = str(cancel.get("boundary") or "")
    external = str(cancel.get("external_status") or "")
    external_label = _EXTERNAL_CANCEL_LABELS.get(external, external or "未接入")
    parts = [
        f"**本地状态：{label}**" if label else "",
        f"商户侧：{external_label}",
        boundary,
    ]
    return "\n".join(part for part in parts if part)


def render_text(outbound: OutboundMessage) -> str:
    """纯文本形态：会话层文案 + 计划卡逐行 + 前置条件（回执不再重复「需要确认」）。"""
    receipt = outbound.kind == KIND_RECEIPT
    parts: list[str] = [outbound.text]
    cancel = card_subscription_cancel(outbound)
    if cancel is not None:
        parts.append(_subscription_cancel_text(cancel))
    lines = card_lines(outbound)
    if lines:
        parts.append("【计划卡】\n" + "\n".join(f"- {line}" for line in lines))
    requires = card_requires(outbound)
    if requires and not receipt:
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
    """飞书交互卡片：计划卡按等级出标题，**执行回执按执行结果出标题**。

    回执（`KIND_RECEIPT`）不再显示「需确认」与前置条件——操作已经执行完，
    再挂一个待确认标题会让用户误以为没生效；取消订阅回执另带本地 / 商户侧状态与边界。
    """
    receipt = outbound.kind == KIND_RECEIPT
    elements: list[dict[str, object]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": outbound.text}},
    ]
    cancel = card_subscription_cancel(outbound)
    if cancel is not None:
        elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": _subscription_cancel_text(cancel),
                },
            }
        )
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
    if requires and not receipt:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "需要：" + "、".join(requires)},
                ],
            }
        )
    if receipt:
        template = _RECEIPT_TEMPLATES.get(outbound.state, _DEFAULT_TEMPLATE)
        title = RECEIPT_TITLES.get(outbound.state, _RECEIPT_DEFAULT_TITLE)
    else:
        template = _FEISHU_TEMPLATES.get(outbound.level or "", _DEFAULT_TEMPLATE)
        title = LEVEL_TITLES.get(outbound.level or "", _DEFAULT_TITLE)
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
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
