"""受控写入的事务内前置条件。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from ai_banking_copilot.gateway.errors import GatewayError


class PreconditionStatus(StrEnum):
    ALLOW = "allow"
    REAUTHORIZE = "reauthorize"
    TAKEOVER = "takeover"
    DENY = "deny"


@dataclass(frozen=True)
class PreconditionResult:
    status: PreconditionStatus
    level: int = 0
    reasons: tuple[str, ...] = ()
    limit_cents: int = 0
    accumulated_cents: int = 0
    message: str = ""


BeforeWrite = Callable[[sqlite3.Connection], PreconditionResult]


class WritePreconditionError(GatewayError):
    def __init__(self, result: PreconditionResult) -> None:
        self.result = result
        super().__init__(result.message or result.status.value)
