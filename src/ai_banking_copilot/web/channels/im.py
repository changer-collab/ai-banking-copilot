"""IM 渠道适配器（**能力子集**，非等价实现；**未接任何真实 IM 平台**）。

两条并存的能力：

- `ImChannel.handle / confirm`：**受信任文本进 / 文本出**的最小搬运，复用既有
  `web.session.Session`——不复制业务逻辑、不判级、不组装计划、不执行，
  也不直接触碰 `CoreBank`（H2：执行只经 `Session` → `policy.pep.Pep`）；
- `ImChannel.parse / render / verify`：把本渠道接入 `web.channels.dispatcher.Dispatcher`
  的统一流水线（幂等 / 限流 / 身份 / 审计）。两者共用**同一个** `SessionManager`，
  故 `GET /im` 与 Web 页面看到的是同一套状态机。

**能力子集声明**：只覆盖「单轮文本交互 + 计划卡确认」，**不是**真实 IM 平台的等价实现——
不覆盖真实 IM 的鉴权 / 群聊 / 富媒体 / 事件订阅 / 长连接推送等；亦未与任何真实 IM 平台对接。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ai_banking_copilot.web.app import AppDeps, build_session_manager
from ai_banking_copilot.web.channels.contracts import (
    CHANNEL_LOCAL,
    InboundMessage,
    OutboundMessage,
)
from ai_banking_copilot.web.channels.render import render_text
from ai_banking_copilot.web.session import Session
from ai_banking_copilot.web.session_manager import SessionManager

DEFAULT_IM_SESSION_ID = "im-default"
"""缺省 IM 会话标识（每个 `ImChannel` 持有独立会话，互不串扰）。"""


@dataclass(frozen=True)
class TextReply:
    """一次文本交互的回复：只回服务端会话状态与文案，**不生成任何新事实**。"""

    session_id: str
    state: str
    text: str


class ImChannel:
    """最小 IM 渠道：受信任文本进 → 既有 `Session` → 文案出。**能力子集**。

    `deps` 由组合根注入（本层**不装配**写面）；每个实例持有一个独立的 `Session`，
    故不同 `session_id` 的 IM 交互互不串扰。`manager` 由 `create_app` 传入时与 Web 共用。
    """

    name = CHANNEL_LOCAL
    """渠道标识（与 `contracts.CHANNEL_LOCAL` 一致，供调度层与审计使用）。"""

    def __init__(
        self,
        deps: AppDeps,
        *,
        session_id: str = DEFAULT_IM_SESSION_ID,
        manager: SessionManager[Session] | None = None,
    ) -> None:
        self._session_id = session_id
        self._manager = manager if manager is not None else build_session_manager(deps)
        self._session = self._manager.get(deps.user_id, session_id)

    @property
    def session_id(self) -> str:
        """本渠道实例的会话标识。"""
        return self._session_id

    def handle(self, text: str) -> TextReply:
        """受信任文本进：交给会话层，回报下一步提示文案（拒绝 / 澄清亦如实回报）。"""
        view = self._session.submit(text)
        return TextReply(session_id=self._session_id, state=view.state.value, text=view.message)

    def confirm(self, card_id: str, frozen_hash: str) -> TextReply:
        """确认：复用会话层的冻结校验（哈希不符 / 状态不允许即**如实抛错**，不谎报成功）。"""
        view = self._session.confirm(card_id, frozen_hash=frozen_hash)
        return TextReply(session_id=self._session_id, state=view.state.value, text=view.message)

    # --- `ChannelAdapter` 协议（供 Dispatcher 统一流水线使用） ----------------

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        """本地 IM 无平台签名：**如实**返回 `True`，并把「无验签」写在能力子集声明里。

        真实平台（飞书）必须实现验签；本方法不得被当作「安全已校验」的依据。
        """
        return True

    def parse(self, payload: Mapping[str, object]) -> InboundMessage:
        """解析本地 IM 入站报文：`text` 必填，`user_id / chat_id / event_id` 可选。"""
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("inbound_text_missing")
        return InboundMessage(
            channel=CHANNEL_LOCAL,
            external_user_id=_optional_text(payload.get("user_id")),
            external_chat_id=_optional_text(payload.get("chat_id")),
            event_id=_optional_text(payload.get("event_id")),
            text=text,
            raw=payload,
        )

    def render(self, outbound: OutboundMessage) -> Mapping[str, object]:
        """渲染成 IM 文本形态（计划卡逐行展开）。"""
        return {
            "channel": outbound.channel,
            "session_id": outbound.session_id,
            "state": outbound.state,
            "level": outbound.level,
            "text": render_text(outbound),
        }


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("channel_field_must_be_text")
    return value
