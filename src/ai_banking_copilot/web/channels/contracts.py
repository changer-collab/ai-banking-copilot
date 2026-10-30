"""渠道消息契约：**渠道只搬消息**，不判级、不组装计划、不执行。

本模块只有纯数据结构与适配器协议，没有任何业务判断：

- `InboundMessage` / `ConfirmCommand`：外部渠道进来的两类请求（发一句话 / 确认计划卡）；
- `OutboundMessage`：渠道回出去的**唯一**形态，`card` 直接取自服务端 `_card_payload`，
  故三端（Web / IM / 飞书）看到的是**同一份事实**；
- `ChannelAdapter`：平台适配器协议（`verify / parse / render`），由 M2 的飞书实现填充；
- 稳定错误族：`ChannelUnauthorized / ChannelRateLimited`，由 Web 路由映射成 401 / 429，
  **不得**在渠道层吞掉后假装成功。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

CHANNEL_LOCAL = "im"
"""本地 IM 渠道（演示用；身份来自服务端 `deps.user_id`）。"""

CHANNEL_FEISHU = "feishu"
"""飞书渠道（M2 接入；**任何外部 id 都必须先完成绑定**）。"""

SUPPORTED_CHANNELS: tuple[str, ...] = (CHANNEL_LOCAL, CHANNEL_FEISHU)

KIND_REPLY = "reply"
KIND_RECEIPT = "receipt"
KIND_ERROR = "error"

MAX_INBOUND_TEXT_LENGTH = 500
"""单条入站文本上界：超长即拒绝，避免把渠道当批量导入用。"""


class ChannelError(RuntimeError):
    """渠道层稳定错误基类；`status_code` 由 Web 路由直接采用。"""

    code = "channel_error"
    status_code = 400


class ChannelUnauthorized(ChannelError):
    """外部身份未绑定（或服务端身份未配置）：**fail-closed**，不放行到会话层。"""

    code = "identity_unbound"
    status_code = 401


class ChannelRateLimited(ChannelError):
    """令牌桶打满：渠道层直接 429，不排队、不静默吞掉用户消息。"""

    code = "rate_limited"
    status_code = 429


@dataclass(frozen=True)
class ChannelEnvelope:
    """两类入站请求的公共头：渠道 / 外部身份 / 幂等键都在这里。"""

    channel: str = CHANNEL_LOCAL
    external_user_id: str | None = None
    external_chat_id: str | None = None
    event_id: str | None = None

    def __post_init__(self) -> None:
        if self.channel not in SUPPORTED_CHANNELS:
            raise ValueError(f"unsupported_channel:{self.channel}")
        if self.external_user_id is not None and not self.external_user_id.strip():
            raise ValueError("external_user_id_empty")
        if self.external_chat_id is not None and not self.external_chat_id.strip():
            raise ValueError("external_chat_id_empty")


@dataclass(frozen=True)
class InboundMessage(ChannelEnvelope):
    """一条入站文本。`raw` 只存平台原文，供排障；业务一律只读 `text`。"""

    text: str = ""
    raw: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.text.strip():
            raise ValueError("inbound_text_empty")
        if len(self.text) > MAX_INBOUND_TEXT_LENGTH:
            raise ValueError("inbound_text_too_long")


@dataclass(frozen=True)
class ConfirmCommand(ChannelEnvelope):
    """一次确认请求：`card_id + frozen_hash` 原样交给会话层做冻结校验。"""

    card_id: str = ""
    frozen_hash: str = ""
    verification_code: str | None = None
    steps: int | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.card_id:
            raise ValueError("card_id_empty")
        if len(self.frozen_hash) != 64:
            raise ValueError("frozen_hash_must_be_sha256")


@dataclass(frozen=True)
class OutboundMessage:
    """渠道出站消息：`text` 取自会话层 `message`，`card` 取自服务端计划卡。"""

    channel: str
    chat_id: str
    session_id: str
    state: str
    text: str
    card: Mapping[str, object] | None = None
    level: str | None = None
    kind: str = KIND_REPLY
    payload: Mapping[str, object] | None = None
    replayed: bool = False


@runtime_checkable
class ChannelAdapter(Protocol):
    """平台适配器：**只做协议搬运**（验签 / 解析 / 渲染），不碰裁决与执行。"""

    name: str

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        """校验平台签名；不通过必须返回 `False`（由调用方拒绝）。"""
        ...

    def parse(self, payload: Mapping[str, object]) -> InboundMessage:
        """把平台报文解析成 `InboundMessage`；字段缺失即抛 `ValueError`。"""
        ...

    def render(self, outbound: OutboundMessage) -> Mapping[str, object]:
        """把统一出站消息渲染成平台格式。"""
        ...
