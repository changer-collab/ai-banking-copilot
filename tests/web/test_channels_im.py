"""P2-7 IM 渠道适配器：受信任文本进 / 出，走既有 `Session`（**能力子集**）。"""

from __future__ import annotations

from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.channels.im import ImChannel


def test_im_channel_handles_trusted_text(deps: AppDeps) -> None:
    channel = ImChannel(deps, session_id="im-a")
    reply = channel.handle("转给李娜 500 元")
    assert reply.session_id == "im-a"
    assert reply.state == "awaiting_confirm"
    assert "计划卡" in reply.text


def test_im_channel_reports_refusal_truthfully(deps: AppDeps) -> None:
    reply = ImChannel(deps, session_id="im-b").handle("帮我订一张去上海的机票")
    assert reply.state == "blocked"
    assert "安全拒绝" in reply.text


def test_im_channels_with_distinct_ids_do_not_interfere(deps: AppDeps) -> None:
    first = ImChannel(deps, session_id="im-1")
    second = ImChannel(deps, session_id="im-2")
    assert first.handle("转给李娜 500 元").state == "awaiting_confirm"
    assert second.handle("查一下上月账单").state == "awaiting_confirm"
    assert first.session_id != second.session_id
