"""仅供 CoreBank 写前回调使用的同连接只读事实视图。"""

from __future__ import annotations

import sqlite3
from datetime import date

from ai_banking_copilot.gateway.errors import GatewayNotFoundError
from ai_banking_copilot.gateway.facts import (
    Record,
    _asset_band_limit_cents,
    _optional_text,
    _record,
)
from ai_banking_copilot.gateway.models import Hold, Money, Payee, Subscription


class TransactionFacts:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def payee(self, payee_id: str) -> Record[Payee]:
        row = self._conn.execute("SELECT * FROM payees WHERE payee_id = ?", (payee_id,)).fetchone()
        if row is None:
            raise GatewayNotFoundError(f"payees 中不存在 payee_id={payee_id}")
        payload = Payee(
            payee_id=str(row["payee_id"]),
            user_id=str(row["user_id"]),
            full_name=str(row["full_name"]),
            account_tail=str(row["account_tail"]),
            phone_tail=str(row["phone_tail"]),
            identity_confirmed=bool(row["identity_confirmed"]),
            blocked=bool(row["blocked"]),
            phone=_optional_text(row["phone"]),
        )
        return _record("payees", payload.payee_id, payload)

    def subscription(self, subscription_id: str) -> Record[Subscription]:
        row = self._conn.execute(
            "SELECT * FROM subscriptions WHERE subscription_id = ?", (subscription_id,)
        ).fetchone()
        if row is None:
            raise GatewayNotFoundError(f"subscriptions 中不存在 subscription_id={subscription_id}")
        payload = Subscription(
            subscription_id=str(row["subscription_id"]),
            user_id=str(row["user_id"]),
            merchant=str(row["merchant"]),
            amount=Money(int(row["amount_cents"])),
            period=str(row["period"]),
            next_charge_on=date.fromisoformat(str(row["next_charge_on"])),
            status=str(row["status"]),
        )
        return _record("subscriptions", payload.subscription_id, payload)

    def hold(self, hold_id: str) -> Record[Hold]:
        row = self._conn.execute(
            "SELECT * FROM holds WHERE hold_id = ?", (hold_id,)
        ).fetchone()
        if row is None:
            raise GatewayNotFoundError(f"holds 中不存在 hold_id={hold_id}")
        payload = Hold(
            hold_id=str(row["hold_id"]),
            account_id=str(row["account_id"]),
            amount=Money(int(row["amount_cents"])),
            reason=str(row["reason"]),
            status=str(row["status"]),
        )
        return _record("holds", payload.hold_id, payload)

    def daily_transfer_total(self, user_id: str, on: date) -> Money:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(-amount_cents), 0) AS total FROM transactions"
            " WHERE user_id = ? AND category = '转账' AND direction = 'out' AND booked_on = ?",
            (user_id, on.isoformat()),
        ).fetchone()
        return Money(int(row["total"]))

    def completed_transfer_total(
        self, user_id: str, on: date, record_ids: tuple[str, ...]
    ) -> Money:
        txn_ids = tuple(value.removeprefix("transactions:") for value in record_ids
                        if value.startswith("transactions:"))
        if not txn_ids:
            return Money(0)
        placeholders = ", ".join("?" for _ in txn_ids)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(-amount_cents), 0) AS total FROM transactions"
            " WHERE user_id = ? AND category = '转账' AND direction = 'out' AND booked_on = ?"
            f" AND txn_id IN ({placeholders})",
            (user_id, on.isoformat(), *txn_ids),
        ).fetchone()
        return Money(int(row["total"]))

    def daily_transfer_limit(self, user_id: str) -> Money:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(balance_cents), 0) AS total FROM accounts WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return Money(_asset_band_limit_cents(int(row["total"])))

    def global_breaker_open(self) -> bool:
        table = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'breaker_state'"
        ).fetchone()
        if table is None:
            return False
        row = self._conn.execute("SELECT state FROM breaker_state LIMIT 1").fetchone()
        return row is not None and str(row["state"]) == "open"
