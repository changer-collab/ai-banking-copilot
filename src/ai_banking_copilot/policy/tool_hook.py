"""统一金融写操作 Pre-Tool Hook：所有工具调用进入业务写服务之前的服务端强制拦截。

定位（为什么不另建框架）
------------------------
本仓库唯一的工具执行入口是 `policy.pep.Pep.execute`（HTTP 层没有直写端点，IM 复用
`Session` 同一确认链），因此 Hook 直接**扩展** PEP，而不是再造一套执行器：
`Pep.execute` 在节点级执行门（`_assert_member`）之后、写处理器派发之前调用本模块。

分类按**契约语义**、不按动作名
------------------------------
`ToolSpec.is_pure_query` 是目录不变式（⟺ `risk is READ_ONLY`）的唯一权威来源：
- 只读动作（账单 / 明细 / 订阅识别 / 理财查询与比较）：沿用既有认证、权限与裁决链，
  不额外拦截，因此「推荐哪款理财」不会被误判成交易；
- 金融写动作（转账 / 付款 / 理财申购与赎回 / 新增或取消订阅 / 其他不可逆副作用）：
  必须携带服务端签发的一次性授权凭据 `WriteGrant`。

不可伪造的凭据
--------------
`WriteGrant` 只能由 `Pep.issue_write_grant` 在**用户确认之后**的服务端路径签发
（`Session._execute` 每次都在 `_require_confirmation` 通过后逐节点签发），并把
「用户 + 会话 + 执行批次 + 计划 ID/哈希 + 动作 + 参数指纹」全部绑定进去。
凭据一次性消费且带短 TTL：重复使用、改参数、换计划、换会话一律拒绝。
模型输出、前端请求体、IM 消息都构造不出凭据，因此无法用提示词伪造「已确认」。
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.policy.audit import KIND_TOOL_HOOK, AuditEvent, AuditLog
from ai_banking_copilot.tools.registry import ToolSpec

DEFAULT_GRANT_TTL_SECONDS = 120.0
"""凭据有效期：一次确认只覆盖紧接着的一次节点执行。"""


class ToolClass(StrEnum):
    """工具类别（由契约语义派生，见模块文档）。"""

    READ_ONLY = "read_only"
    FINANCIAL_WRITE = "financial_write"


class HookStatus(StrEnum):
    """Hook 对外返回的明确状态（`allowed` 之外均为拒绝）。"""

    ALLOWED = "allowed"
    CONFIRMATION_REQUIRED = "confirmation_required"
    AUTHORIZATION_REQUIRED = "authorization_required"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTION_FAILED = "execution_failed"


class ToolHookBlocked(RuntimeError):
    """写操作被 Hook 拦截（fail-closed，业务写服务从未被调用）。"""

    def __init__(
        self, status: HookStatus, action: str, reason: str, *, plan_id: str = ""
    ) -> None:
        self.status = status
        self.action = action
        self.reason = reason
        self.plan_id = plan_id
        super().__init__(f"[{status.value}] {reason}")


def classify(spec: ToolSpec) -> ToolClass:
    """按契约语义分类（不看动作名）。`is_pure_query` 是目录不变式的权威来源。"""
    return ToolClass.READ_ONLY if spec.is_pure_query else ToolClass.FINANCIAL_WRITE


def operation_of(spec: ToolSpec) -> str:
    """操作类型（审计用）：优先净效应类别 `kind`，缺省回落到工具类别。"""
    kind = spec.kind
    if kind is None:
        return classify(spec).value
    return getattr(kind, "value", str(kind))


@dataclass(frozen=True)
class WriteGrant:
    """一次性写操作授权凭据：把「这一次确认」绑定到确切的计划与参数。"""

    grant_id: str
    session_id: str
    execution_id: str
    user_id: str
    plan_id: str
    plan_hash: str
    action: str
    member: str
    approved_level: Level
    strong_verified: bool
    issued_at: float
    expires_at: float


class GrantLedger:
    """授权凭据台账（进程内、一次性消费）。只有 `Pep.issue_write_grant` 会写入。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_GRANT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._grants: dict[str, WriteGrant] = {}

    def now(self) -> float:
        return self._clock()

    def issue(
        self,
        *,
        session_id: str,
        execution_id: str,
        user_id: str,
        plan_id: str,
        plan_hash: str,
        action: str,
        member: str,
        approved_level: Level,
        strong_verified: bool,
    ) -> WriteGrant:
        now = self._clock()
        grant = WriteGrant(
            grant_id=secrets.token_urlsafe(24),
            session_id=session_id,
            execution_id=execution_id,
            user_id=user_id,
            plan_id=plan_id,
            plan_hash=plan_hash,
            action=action,
            member=member,
            approved_level=approved_level,
            strong_verified=strong_verified,
            issued_at=now,
            expires_at=now + self._ttl,
        )
        self._grants[grant.grant_id] = grant
        self._prune(now)
        return grant

    def redeem(self, grant_id: str) -> WriteGrant | None:
        """取出并**消费**凭据；不存在（含已被消费）返回 `None`。"""
        return self._grants.pop(grant_id, None)

    def _prune(self, now: float) -> None:
        expired = [key for key, grant in self._grants.items() if grant.expires_at <= now]
        for key in expired:
            self._grants.pop(key, None)


class PreToolHook:
    """工具执行前的统一拦截器：分类 → 只读直通 / 写操作凭据校验 → 审计落链。"""

    def __init__(self, *, ledger: GrantLedger, audit: AuditLog) -> None:
        self._ledger = ledger
        self._audit = audit

    @property
    def ledger(self) -> GrantLedger:
        return self._ledger

    def block(
        self,
        spec: ToolSpec,
        *,
        status: HookStatus,
        reason: str,
        plan_id: str = "",
        plan_hash: str = "",
        user_id: str = "",
        session_id: str = "",
        execution_id: str = "",
    ) -> ToolHookBlocked:
        """构造一次拦截并落审计（不抛出，由调用方 `raise`）。不含任何凭证 / 敏感值。"""
        self._audit.append(
            AuditEvent(
                event_id=f"hook:{spec.action}:{secrets.token_hex(8)}",
                kind=KIND_TOOL_HOOK,
                payload={
                    "tool": spec.action,
                    "op_type": operation_of(spec),
                    "tool_class": classify(spec).value,
                    "plan_id": plan_id,
                    "plan_hash": plan_hash,
                    "block_reason": reason,
                    "confirmation_state": status.value,
                    "result": "blocked",
                    "user_id": user_id,
                    "session_id": session_id,
                    "execution_id": execution_id,
                },
                prev_hash="",
            )
        )
        return ToolHookBlocked(status, spec.action, reason, plan_id=plan_id)

    def authorize(
        self,
        spec: ToolSpec,
        *,
        member: str,
        plan_id: str,
        plan_hash: str,
        approved_level: Level | None,
        grant_id: str,
        user_id: str,
        session_id: str,
        execution_id: str,
    ) -> WriteGrant | None:
        """写操作前置门：校验凭据与「用户 / 会话 / 计划 / 参数指纹」的全部绑定。

        只读动作不做任何拦截（返回 `None`）。任一绑定不匹配即 `ToolHookBlocked`。
        """
        if classify(spec) is ToolClass.READ_ONLY:
            return None

        def deny(status: HookStatus, reason: str) -> ToolHookBlocked:
            return self.block(
                spec, status=status, reason=reason, plan_id=plan_id, plan_hash=plan_hash,
                user_id=user_id, session_id=session_id, execution_id=execution_id,
            )

        if not grant_id:
            raise deny(HookStatus.CONFIRMATION_REQUIRED, "写操作缺少服务端确认凭据，禁止直接执行")
        grant = self._ledger.redeem(grant_id)
        if grant is None:
            raise deny(HookStatus.CONFIRMATION_REQUIRED, "确认凭据不存在或已被使用，请重新确认计划")
        if grant.expires_at <= self._ledger.now():
            raise deny(HookStatus.EXPIRED, "确认凭据已过期，请重新确认计划")
        if grant.action != spec.action or grant.member != member:
            raise deny(HookStatus.REJECTED, "调用参数与已确认计划不一致，原确认已失效")
        if grant.plan_id != plan_id or grant.plan_hash != plan_hash:
            raise deny(HookStatus.REJECTED, "确认凭据与冻结计划不匹配，原确认已失效")
        if (
            grant.user_id != user_id
            or grant.session_id != session_id
            or grant.execution_id != execution_id
        ):
            raise deny(HookStatus.AUTHORIZATION_REQUIRED, "确认凭据与当前用户或会话不匹配")
        if approved_level is not None and grant.approved_level < approved_level:
            raise deny(
                HookStatus.AUTHORIZATION_REQUIRED,
                f"确认级别低于当前裁决：{approved_level.name}",
            )
        return grant
