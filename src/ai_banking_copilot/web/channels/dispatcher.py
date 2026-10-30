"""渠道调度：所有外部入口共用的**一条**流水线。

固定顺序（每一步都不可省略，跳步即视为缺陷）：

   幂等 → 限流 → 身份 → 取会话 → 提交/确认 → 组装出站 → 审计 → 回填幂等

纪律：

- **渠道只搬消息**：本模块不判级、不组装计划、不执行，一切交给 `web.session.Session`；
- **会话键**用 `f"{channel}:{chat_id}"`，与 Web 共用同一 `SessionManager`，故 IM 与 Web
  对同一用户是**同一套状态机**；
- **身份 fail-closed**：外部 id 必须命中绑定表，否则 401；本地渠道无外部 id 时用服务端身份；
- **审计必带 `channel`**：只落 `text_digest`（不落原文），可追溯而不过度采集；
- **幂等**：同一 `event_id` 第二次直接回放首次结果（`replayed=True`），不重复执行。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import replace
from threading import Lock

from ai_banking_copilot.policy.audit import KIND_CHANNEL, AuditEvent, AuditLog
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.web.channels.contracts import (
    CHANNEL_FEISHU,
    CHANNEL_LOCAL,
    KIND_RECEIPT,
    KIND_REPLY,
    ChannelRateLimited,
    ChannelUnauthorized,
    ConfirmCommand,
    InboundMessage,
    OutboundMessage,
)
from ai_banking_copilot.web.channels.identity import IdentityDirectory
from ai_banking_copilot.web.channels.ratelimit import TokenBucketRateLimiter
from ai_banking_copilot.web.session import (
    ConfirmationMismatchError,
    ExecutionView,
    Session,
    SessionStateError,
    StrongVerificationRequiredError,
    SubmissionView,
)
from ai_banking_copilot.web.session_manager import SessionManager

RATE_LIMIT_PER_MINUTE = 20
"""单用户每分钟入站消息上界（M3 令牌桶的最小实现，超限直接 429）。"""

RATE_LIMIT_WINDOW_S = 60.0
DEFAULT_CHAT_ID = "default"
"""请求未带 `chat_id` 时的会话键后缀（同一渠道下等价于单会话）。"""

_MAX_IDEMPOTENCY_ENTRIES = 500
"""幂等缓存条数上界：超出即淘汰最早一条（演示级，够覆盖重投窗口）。"""

_KNOWN_BUSINESS_REJECTIONS = (
    SessionStateError,
    ConfirmationMismatchError,
    StrongVerificationRequiredError,
    StalePlanError,
)


SlidingWindowRateLimiter = TokenBucketRateLimiter
"""兼容旧导入名；实现已迁移到可靠性模块。"""

LOGGER = logging.getLogger(__name__)


def channel_audit_payload(
    *,
    user_id: str,
    channel: str,
    direction: str,
    status: str,
    reason: str = "",
    event_id: str | None = None,
    external_user_id: str | None = None,
    attempt_no: int | None = None,
    http_status: int | None = None,
    text: str | None = None,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """构造不含原文和凭据的渠道审计摘要。"""
    payload = dict(details or {})
    payload.update({
        "user_id": user_id,
        "channel": channel,
        "direction": direction,
        "status": status,
        "reason": reason,
        "event_id": event_id or "",
        "external_user_id": external_user_id or "",
        "attempt_no": attempt_no,
        "http_status": http_status,
    })
    if text is not None:
        payload["text_digest"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        payload["text_length"] = len(text)
    return payload


class DispatchOutcomeUnknown(RuntimeError):
    """业务调用已经开始，失败时不能证明没有产生副作用。"""


class Dispatcher:
    """渠道统一调度：`InboundMessage` / `ConfirmCommand` → `OutboundMessage`。"""

    def __init__(
        self,
        *,
        manager: SessionManager[Session],
        identities: IdentityDirectory,
        submission_payload: Callable[[SubmissionView], dict[str, object]],
        execution_payload: Callable[[ExecutionView], dict[str, object]],
        audit: AuditLog,
        server_user_id: str,
        limiter: TokenBucketRateLimiter | None = None,
        feishu_configured: bool = False,
        feishu_audit_channel: str = CHANNEL_FEISHU,
    ) -> None:
        self._manager = manager
        self._identities = identities
        self._submission_payload = submission_payload
        self._execution_payload = execution_payload
        self._audit = audit
        self._server_user_id = server_user_id
        self._limiter = limiter if limiter is not None else TokenBucketRateLimiter()
        self._feishu_configured = feishu_configured
        self._feishu_audit_channel = feishu_audit_channel
        self._cache: OrderedDict[str, OutboundMessage] = OrderedDict()
        self._cache_lock = Lock()

    def dispatch(self, inbound: InboundMessage | ConfirmCommand) -> OutboundMessage:
        """执行流水线；会话层异常（状态 / 冻结校验）**继续上抛**，由路由映射 409。"""
        return self._dispatch(inbound, preserve_outcome_error=False)

    def dispatch_live(
        self, inbound: InboundMessage | ConfirmCommand
    ) -> OutboundMessage:
        """为异步外部回调执行；业务开始后的异常标记为结果未知。"""
        return self._dispatch(inbound, preserve_outcome_error=True)

    def resume_green(
        self, inbound: InboundMessage, submission: OutboundMessage
    ) -> OutboundMessage:
        """绿卡自动执行：同一条入站事件的内部续跑，**不重复计频、不新增入站事件**。

        Web 端是浏览器在拿到绿卡后再点一次确认；IM 没有前端，由调度层替用户完成这一步。
        绿卡按策略只可能是纯查询，因此这里不放宽任何写操作口径。
        """
        card = submission.card or {}
        card_id = card.get("card_id")
        frozen_hash = card.get("frozen_hash")
        if not isinstance(card_id, str) or not isinstance(frozen_hash, str):
            return submission
        user_id = self._resolve_user(inbound)
        chat_id = inbound.external_chat_id or inbound.external_user_id or DEFAULT_CHAT_ID
        session_id = f"{inbound.channel}:{chat_id}"
        execution = self._manager.get(user_id, session_id).confirm(
            card_id, frozen_hash=frozen_hash
        )
        payload = self._execution_payload(execution)
        outbound = _outbound(
            inbound, session_id=session_id, payload=payload, kind=KIND_RECEIPT
        )
        self._record_audit(inbound, outbound, user_id=user_id)
        self._remember(inbound, outbound)
        return outbound

    def _dispatch(
        self,
        inbound: InboundMessage | ConfirmCommand,
        *,
        preserve_outcome_error: bool,
    ) -> OutboundMessage:
        try:
            user_id = self._resolve_user(inbound)
        except ChannelUnauthorized:
            self._record_failure_best_effort(
                inbound,
                user_id=self._server_user_id,
                status="rejected",
                reason="identity_unbound",
            )
            self._log_failure(inbound, status="rejected", reason="identity_unbound")
            raise
        except Exception:
            self._record_failure_best_effort(
                inbound,
                user_id=self._server_user_id,
                status="failed",
                reason="dispatch_failed",
            )
            self._log_failure(inbound, status="failed", reason="dispatch_failed")
            raise
        cached = self._cached(inbound)
        if cached is not None:
            return replace(cached, replayed=True)
        rate_identity = inbound.external_user_id or user_id
        if not self._limiter.allow(
            f"{inbound.channel}:{rate_identity}", reservation_id=inbound.event_id
        ):
            self._record_failure_best_effort(
                inbound,
                user_id=user_id,
                status="rejected",
                reason="rate_limited",
            )
            self._log_failure(inbound, status="rejected", reason="rate_limited")
            raise ChannelRateLimited("消息过于频繁，请稍后再试")

        chat_id = inbound.external_chat_id or inbound.external_user_id or DEFAULT_CHAT_ID
        session_id = f"{inbound.channel}:{chat_id}"
        session = self._manager.get(user_id, session_id)

        business_completed = False
        try:
            if isinstance(inbound, ConfirmCommand):
                execution = session.confirm(
                    inbound.card_id,
                    frozen_hash=inbound.frozen_hash,
                    verification_code=inbound.verification_code,
                    steps=inbound.steps,
                )
                business_completed = True
                payload = self._execution_payload(execution)
                kind = KIND_RECEIPT
            else:
                submission = session.submit(inbound.text)
                business_completed = True
                payload = self._submission_payload(submission)
                kind = KIND_REPLY

            outbound = _outbound(
                inbound, session_id=session_id, payload=payload, kind=kind
            )
            self._record_audit(inbound, outbound, user_id=user_id)
            self._remember(inbound, outbound)
            return outbound
        except Exception as error:
            if not business_completed:
                self._record_failure_best_effort(
                    inbound,
                    user_id=user_id,
                    status="failed",
                    reason="dispatch_failed",
                )
                self._log_failure(inbound, status="failed", reason="dispatch_failed")
            if business_completed or (
                preserve_outcome_error
                and not isinstance(error, _KNOWN_BUSINESS_REJECTIONS)
            ):
                raise DispatchOutcomeUnknown("dispatch_outcome_unknown") from error
            raise

    def health(self) -> dict[str, object]:
        """渠道健康页：如实区分「已配置 / 未配置」，不把 stub 说成可用。"""
        return {
            "channels": {
                CHANNEL_LOCAL: {
                    "configured": True,
                    "mode": "local",
                    "bound_identities": self._identities.binding_count(),
                },
                CHANNEL_FEISHU: {
                    "configured": self._feishu_configured,
                    "mode": "adapter" if self._feishu_configured else "stub",
                    "reason": "" if self._feishu_configured else "not_configured",
                },
            },
            "rate_limit": {
                "limit": RATE_LIMIT_PER_MINUTE,
                "window_seconds": RATE_LIMIT_WINDOW_S,
            },
        }

    # --- 流水线各步 -------------------------------------------------------

    def _resolve_user(self, inbound: InboundMessage | ConfirmCommand) -> str:
        external = inbound.external_user_id
        if external is None:
            if inbound.channel == CHANNEL_LOCAL and self._server_user_id:
                return self._server_user_id
            raise ChannelUnauthorized("缺少已绑定的外部身份")
        binding = self._identities.resolve(inbound.channel, external)
        if binding is None:
            raise ChannelUnauthorized("外部身份未绑定，请先完成绑定")
        return binding.user_id

    @staticmethod
    def _idempotency_key(inbound: InboundMessage | ConfirmCommand) -> str | None:
        if not inbound.event_id:
            return None
        return f"{inbound.channel}:{inbound.event_id}"

    def _cached(self, inbound: InboundMessage | ConfirmCommand) -> OutboundMessage | None:
        key = self._idempotency_key(inbound)
        if key is None:
            return None
        with self._cache_lock:
            return self._cache.get(key)

    def _remember(
        self, inbound: InboundMessage | ConfirmCommand, outbound: OutboundMessage
    ) -> None:
        key = self._idempotency_key(inbound)
        if key is None:
            return
        with self._cache_lock:
            self._cache[key] = outbound
            while len(self._cache) > _MAX_IDEMPOTENCY_ENTRIES:
                self._cache.popitem(last=False)

    def _record_audit(
        self,
        inbound: InboundMessage | ConfirmCommand,
        outbound: OutboundMessage,
        *,
        user_id: str,
    ) -> None:
        """只落摘要：渠道 / 会话 / 状态 / 等级 / 卡片标识 / 文本摘要，**不落原文**。"""
        if isinstance(inbound, ConfirmCommand):
            material = f"{inbound.card_id}:{inbound.frozen_hash}"
            request_kind = "confirm"
            card_id = inbound.card_id
        else:
            material = inbound.text
            request_kind = "message"
            card_id = str((outbound.card or {}).get("card_id", ""))
        self._audit.append(
            AuditEvent(
                event_id=f"{KIND_CHANNEL}:{self._audit_channel(inbound)}:{secrets.token_hex(8)}",
                kind=KIND_CHANNEL,
                payload=channel_audit_payload(
                    user_id=user_id,
                    channel=self._audit_channel(inbound),
                    direction="inbound",
                    status="sent",
                    event_id=inbound.event_id,
                    external_user_id=inbound.external_user_id,
                    text=material,
                    details={
                        "session_id": outbound.session_id,
                        "request_kind": request_kind,
                        "state": outbound.state,
                        "level": outbound.level or "",
                        "card_id": card_id,
                        "frozen_hash": (
                            inbound.frozen_hash
                            if isinstance(inbound, ConfirmCommand)
                            else ""
                        ),
                    },
                ),
                prev_hash="",
            )
        )

    def _record_failure(
        self,
        inbound: InboundMessage | ConfirmCommand,
        *,
        user_id: str,
        status: str,
        reason: str,
    ) -> None:
        material = (
            f"{inbound.card_id}:{inbound.frozen_hash}"
            if isinstance(inbound, ConfirmCommand)
            else inbound.text
        )
        self._audit.append(
            AuditEvent(
                event_id=(
                    f"{KIND_CHANNEL}:{self._audit_channel(inbound)}:"
                    f"{secrets.token_hex(8)}"
                ),
                kind=KIND_CHANNEL,
                payload=channel_audit_payload(
                    user_id=user_id,
                    channel=self._audit_channel(inbound),
                    direction="inbound",
                    status=status,
                    reason=reason,
                    event_id=inbound.event_id,
                    external_user_id=inbound.external_user_id,
                    text=material,
                    details={
                        "request_kind": (
                            "confirm" if isinstance(inbound, ConfirmCommand) else "message"
                        )
                    },
                ),
                prev_hash="",
            )
        )

    def _record_failure_best_effort(
        self,
        inbound: InboundMessage | ConfirmCommand,
        *,
        user_id: str,
        status: str,
        reason: str,
    ) -> None:
        try:
            self._record_failure(
                inbound,
                user_id=user_id,
                status=status,
                reason=reason,
            )
        except Exception as error:
            LOGGER.warning(
                "channel_event channel=%s event_id=%s status=audit_failed "
                "reason=channel_audit_failed error_type=%s",
                self._audit_channel(inbound),
                inbound.event_id or "",
                type(error).__name__,
            )

    def _audit_channel(self, inbound: InboundMessage | ConfirmCommand) -> str:
        return (
            self._feishu_audit_channel
            if inbound.channel == CHANNEL_FEISHU
            else inbound.channel
        )

    def _log_failure(
        self,
        inbound: InboundMessage | ConfirmCommand,
        *,
        status: str,
        reason: str,
    ) -> None:
        LOGGER.warning(
            "channel_event channel=%s event_id=%s status=%s reason=%s",
            self._audit_channel(inbound),
            inbound.event_id or "",
            status,
            reason,
        )


def _outbound(
    inbound: InboundMessage | ConfirmCommand,
    *,
    session_id: str,
    payload: dict[str, object],
    kind: str,
) -> OutboundMessage:
    card = payload.get("card")
    card_map = card if isinstance(card, dict) else None
    level: str | None = None
    if card_map is not None and card_map.get("level") is not None:
        level = str(card_map["level"])
    if level is None:
        decision = payload.get("decision")
        if isinstance(decision, dict) and decision.get("level") is not None:
            level = str(decision["level"])
    return OutboundMessage(
        channel=inbound.channel,
        chat_id=inbound.external_chat_id or inbound.external_user_id or DEFAULT_CHAT_ID,
        session_id=session_id,
        state=str(payload.get("state", "")),
        text=str(payload.get("message", "")),
        card=card_map,
        level=level,
        kind=kind,
        payload=payload,
    )
