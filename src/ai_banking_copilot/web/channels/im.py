"""IM 渠道适配器（**能力子集**，非等价实现；**未接任何真实 IM 平台**）。

本模块只做「受信任文本进 / 文本出」的**最小**搬运，复用既有 `web.session.Session`：

- **不复制业务逻辑**：不判级、不组装计划、不执行，一切交给会话层与其下游（03 / 04 / 05）；
- **不直接触碰 `CoreBank`**（H2）：执行只经注入的 `Session` → `policy.pep.Pep`，
  补偿经 `policy.reversal.ReversalService`（由组合根注入）；
- `text` 被视为**受信任文本**（来源可信由渠道层负责），本模块不做鉴权。

**能力子集声明**：只覆盖「单轮文本交互」这一最小面，**不是**真实 IM 平台的等价实现——
不覆盖真实 IM 的鉴权 / 群聊 / 富媒体 / 事件订阅 / 长连接推送等；亦未与任何真实 IM 平台对接。
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session

DEFAULT_IM_SESSION_ID = "im-default"
"""缺省 IM 会话标识（每个 `ImChannel` 持有独立 `Session`，互不串扰）。"""


@dataclass(frozen=True)
class TextReply:
    """一次文本交互的回复：只回服务端会话状态与文案，**不生成任何新事实**。"""

    session_id: str
    state: str
    text: str


class ImChannel:
    """最小 IM 渠道：受信任文本进 → 既有 `Session` → 文案出。**能力子集**。

    `deps` 由组合根注入（本层**不装配**写面）；每个实例持有一个独立的 `Session`，
    故不同 `session_id` 的 IM 交互互不串扰。
    """

    def __init__(self, deps: AppDeps, *, session_id: str = DEFAULT_IM_SESSION_ID) -> None:
        self._session_id = session_id
        self._session = Session(deps)

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
