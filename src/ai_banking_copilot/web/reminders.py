"""到期推送：站内由 `/api/due` 承担，本模块只负责把同一条提案推到飞书。

纪律：
- **幂等**：`event_id = push:{user_id}:{schedule_id}:{due_on}:{kind}` 落渠道审计，命中即跳过；
- **只推本人**：收件人是该用户绑定的飞书 `open_id`（私聊），不使用群 `chat_id`；
- **不谎报**：未配置凭据 / replay 模式不真发，落审计 `outcome=replay`。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from ai_banking_copilot.policy.audit import (
    KIND_CHANNEL,
    AuditEvent,
    AuditLog,
    ChainBrokenError,
)
from ai_banking_copilot.web.channels.contracts import CHANNEL_FEISHU, OutboundMessage
from ai_banking_copilot.web.channels.dispatcher import channel_audit_payload
from ai_banking_copilot.web.channels.feishu import FeishuAdapter, FeishuConfig
from ai_banking_copilot.web.life_event_store import PRIVATE_VISIBILITY
from ai_banking_copilot.web.plan_card import PlanCard
from ai_banking_copilot.web.session import Session

if TYPE_CHECKING:
    from ai_banking_copilot.web.app import AppDeps

LOGGER = logging.getLogger(__name__)
TICK_SECONDS = 60

PUSH_KIND = "due_push"
"""审计摘要里的 `request_kind`：用于识别本模块写下的推送记录。"""
PUSH_ID_PREFIX = "push:"
AUDIT_CHANNEL_REPLAY = "replay"
"""replay / 未配置时的审计渠道名：与既有渠道启动口径一致。"""
PUSHED_STATUSES = frozenset({"sent", "replay"})
"""只有这两类审计记录算「已推送」；`failed` 不占用幂等键，下一轮仍可重试。"""


async def run_forever(tick: Callable[[], Awaitable[int]]) -> None:
    """常驻循环：单轮异常只记日志，不让任务退出。"""
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.warning("到期推送一轮失败", exc_info=True)
        await asyncio.sleep(TICK_SECONDS)


def push_due_once(deps: AppDeps, *, adapter: FeishuAdapter | None = None) -> int:
    """扫描一次到期预约并按人推送；返回本轮落审计的推送条数。

    同步实现（内部含 SQLite 读与阻塞 HTTP），调用方应在后台线程里跑，别阻塞事件循环。
    """
    store = deps.life_events
    if store is None:
        return 0
    bindings = _feishu_bindings(deps)
    if not bindings:
        return 0
    pushed = _pushed_keys(deps.audit)
    if pushed is None:
        return 0
    feishu = adapter if adapter is not None else FeishuAdapter(FeishuConfig.from_env())
    live = feishu.configured and not feishu.replay_mode
    audit_channel = CHANNEL_FEISHU if live else AUDIT_CHANNEL_REPLAY
    written = 0
    seen_users: set[str] = set()
    for user_id, open_id in bindings:
        if user_id in seen_users:
            continue
        seen_users.add(user_id)
        pending = [
            item
            for item in store.due_schedules(user_id, today=deps.today)
            # 私密预约不进共享推送：只在用户自己的记录页里折叠显示。
            if store.record_visibility(user_id, item.record_id) != PRIVATE_VISIBILITY
            and _push_key(user_id, item.schedule_id, item.due_on, item.kind) not in pushed
        ]
        if not pending:
            continue
        session_key = f"{CHANNEL_FEISHU}:{open_id}"
        session = Session(
            replace(deps, user_id=user_id, session_repository=None),
            session_id=session_key,
        )
        view = session.check_due()
        card = view.card
        if card is None:
            # 到期预约仍在，但这一轮没有可提案的卡片：不推送、不占用幂等键。
            LOGGER.info("到期推送：没有待办卡片，跳过 user_id=%s", user_id)
            continue
        card_map = _card_map(card)
        outbound = OutboundMessage(
            channel=CHANNEL_FEISHU,
            chat_id=open_id,
            session_id=session_key,
            state=view.state.value,
            text=view.message,
            card=card_map,
            level=card.level.name,
            payload={
                "state": view.state.value,
                "message": view.message,
                "card": card_map,
            },
        )
        if live:
            try:
                feishu.send(outbound, receive_id_type="open_id")
            except Exception as error:  # noqa: BLE001 - 单用户失败不影响其他用户
                LOGGER.warning(
                    "到期推送失败 user_id=%s error_type=%s", user_id, type(error).__name__
                )
                for item in pending:
                    _append_push_audit(
                        deps.audit,
                        user_id=user_id,
                        channel=audit_channel,
                        open_id=open_id,
                        status="failed",
                        reason=type(error).__name__,
                        key=_push_key(user_id, item.schedule_id, item.due_on, item.kind),
                        schedule_id=item.schedule_id,
                        due_on=item.due_on.isoformat(),
                        kind=item.kind,
                        card_id=card.card_id,
                        level=card.level.name,
                    )
                continue
            status, reason = "sent", ""
        else:
            status, reason = "replay", "replay_mode"
        for item in pending:
            _append_push_audit(
                deps.audit,
                user_id=user_id,
                channel=audit_channel,
                open_id=open_id,
                status=status,
                reason=reason,
                key=_push_key(user_id, item.schedule_id, item.due_on, item.kind),
                schedule_id=item.schedule_id,
                due_on=item.due_on.isoformat(),
                kind=item.kind,
                card_id=card.card_id,
                level=card.level.name,
            )
            written += 1
    return written


def _push_key(user_id: str, schedule_id: str, due_on: object, kind: str) -> str:
    """幂等键：同一人、同一预约、同一到期日、同一类型只推一次。"""
    return f"{PUSH_ID_PREFIX}{user_id}:{schedule_id}:{due_on}:{kind}"


def _feishu_bindings(deps: AppDeps) -> list[tuple[str, str]]:
    """已绑定的 `(user_id, open_id)`；无持久化绑定存储时返回空（不推群）。"""
    store = deps.channel_store
    if store is None:
        return []
    rows = store.query(
        "SELECT user_id, external_user_id FROM channel_identities"
        " WHERE channel = 'feishu' ORDER BY user_id, bound_at"
    )
    bindings: list[tuple[str, str]] = []
    for row in rows:
        user_id = str(row["user_id"] or "")
        open_id = str(row["external_user_id"] or "")
        if user_id and open_id:
            bindings.append((user_id, open_id))
    return bindings


def _pushed_keys(audit: AuditLog) -> set[str] | None:
    """已成功落账的推送幂等键；审计链不可读时返回 `None`（fail-closed，宁可少推）。"""
    try:
        events = audit.events()
    except ChainBrokenError:
        LOGGER.warning("到期推送：审计链不可读，本轮跳过（fail-closed）")
        return None
    return {
        str(event.payload.get("event_id") or "")
        for event in events
        if event.kind == KIND_CHANNEL
        and str(event.payload.get("request_kind") or "") == PUSH_KIND
        and str(event.payload.get("status") or "") in PUSHED_STATUSES
    }


def _card_map(card: PlanCard) -> dict[str, object]:
    """计划卡摘要：字段与 `web.app._card_payload` 同口径，渠道层不二次加工事实。"""
    return {
        "card_id": card.card_id,
        "frozen_hash": card.frozen_hash,
        "level": card.level.name,
        "lines": list(card.lines),
        "requires": list(card.requires),
        "expires_at": None if card.expires_at is None else card.expires_at.isoformat(),
    }


def _append_push_audit(
    audit: AuditLog,
    *,
    user_id: str,
    channel: str,
    open_id: str,
    status: str,
    reason: str,
    key: str,
    schedule_id: str,
    due_on: str,
    kind: str,
    card_id: str,
    level: str,
) -> None:
    """落一条渠道审计：只落摘要与幂等键，不落消息原文。"""
    audit.append(
        AuditEvent(
            event_id=f"{KIND_CHANNEL}:{channel}:{secrets.token_hex(8)}",
            kind=KIND_CHANNEL,
            payload=channel_audit_payload(
                user_id=user_id,
                channel=channel,
                direction="outbound",
                status=status,
                reason=reason,
                event_id=key,
                external_user_id=open_id,
                details={
                    "request_kind": PUSH_KIND,
                    "schedule_id": schedule_id,
                    "due_on": due_on,
                    "kind": kind,
                    "card_id": card_id,
                    "level": level,
                },
            ),
            prev_hash="",
        )
    )
