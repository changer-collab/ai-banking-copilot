"""只读事实网关（TCB 的对外读面）。

铁律（H1 事实零生成）：本模块是**数值与实体**的唯一产出地；每条记录都带
`record_id` 与 `content_hash`，上层只允许比对哈希、不得改写数值。
本模块**不含任何写路径**——写只能在 `core_bank.CoreBank`。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Generic, TypeVar

from ai_banking_copilot.gateway.errors import GatewayNotFoundError, ReadOnlyViolation
from ai_banking_copilot.gateway.hashing import canonical_json, sha256_hex
from ai_banking_copilot.gateway.models import (
    Account,
    BillSummary,
    Card,
    CategoryTotal,
    Hold,
    Money,
    Payee,
    Subscription,
    Transaction,
    WealthProduct,
)
from ai_banking_copilot.gateway.store import Store, StoreMode
from ai_banking_copilot.gateway.transfer_store import CollectionRequest, ScheduledTransfer

T = TypeVar("T")

_ASSET_BAND_LIMITS_CENTS: tuple[tuple[int, int], ...] = (
    (10_000_000, 100_000),
    (5_000_000, 80_000),
    (2_000_000, 50_000),
)
"""客户资产分档 → 客户日限额（分）：`(资产下限, 日限额)`，从高到低匹配。

最高档即**官方上限 1000 元**；资产越低限额越严（只加严、不放宽）。"""


def _asset_band_limit_cents(assets_cents: int) -> int:
    for threshold_cents, limit_cents in _ASSET_BAND_LIMITS_CENTS:
        if assets_cents >= threshold_cents:
            return limit_cents
    return 30_000


@dataclass(frozen=True)
class Record(Generic[T]):
    """带出处的事实记录：`record_id` 定位、`content_hash` 防篡改。"""

    record_id: str
    payload: T
    content_hash: str


class FactGateway:
    """只读事实服务。构造即断言只读连接（TCB 写权限独占）。"""

    def __init__(self, store: Store) -> None:
        if store.mode is not StoreMode.READ_ONLY:
            raise ReadOnlyViolation("FactGateway 只接受只读连接：写权限必须留在 CoreBank")
        self._store = store

    def accounts_of(self, user_id: str) -> tuple[Record[Account], ...]:
        rows = self._store.query(
            "SELECT * FROM accounts WHERE user_id = ? ORDER BY account_id", (user_id,)
        )
        return tuple(self._account(row) for row in rows)

    def payees_of(self, user_id: str) -> tuple[Record[Payee], ...]:
        rows = self._store.query(
            "SELECT * FROM payees WHERE user_id = ? ORDER BY payee_id", (user_id,)
        )
        return tuple(self._payee(row) for row in rows)

    def cards_of(self, user_id: str) -> tuple[Record[Card], ...]:
        rows = self._store.query(
            "SELECT * FROM cards WHERE user_id = ? ORDER BY card_id", (user_id,)
        )
        return tuple(self._card(row) for row in rows)

    def subscriptions_of(self, user_id: str) -> tuple[Record[Subscription], ...]:
        rows = self._store.query(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY subscription_id", (user_id,)
        )
        return tuple(self._subscription(row) for row in rows)

    def holds_of(self, account_id: str) -> tuple[Record[Hold], ...]:
        rows = self._store.query(
            "SELECT * FROM holds WHERE account_id = ? ORDER BY hold_id", (account_id,)
        )
        return tuple(self._hold(row) for row in rows)

    def wealth_products(self) -> tuple[Record[WealthProduct], ...]:
        rows = self._store.query("SELECT * FROM wealth_products ORDER BY product_id")
        return tuple(self._wealth_product(row) for row in rows)

    def transactions_between(
        self, account_id: str, start: date, end: date
    ) -> tuple[Record[Transaction], ...]:
        rows = self._store.query(
            "SELECT * FROM transactions WHERE account_id = ? AND booked_on BETWEEN ? AND ?"
            " ORDER BY booked_on, txn_id",
            (account_id, start.isoformat(), end.isoformat()),
        )
        return tuple(self._transaction(row) for row in rows)

    def monthly_bill(self, user_id: str, year: int, month: int) -> Record[BillSummary]:
        prefix = f"{year:04d}-{month:02d}"
        rows = self._store.query(
            "SELECT category, SUM(amount_cents) AS total_cents, COUNT(*) AS txn_count"
            " FROM transactions WHERE user_id = ? AND substr(booked_on, 1, 7) = ?"
            " GROUP BY category ORDER BY category",
            (user_id, prefix),
        )
        by_category = tuple(
            CategoryTotal(
                category=str(row["category"]),
                total=Money(int(row["total_cents"])),
                txn_count=int(row["txn_count"]),
            )
            for row in rows
        )
        expense = sum((-item.total.cents for item in by_category if item.total.cents < 0), 0)
        income = sum((item.total.cents for item in by_category if item.total.cents > 0), 0)
        summary = BillSummary(
            user_id=user_id,
            year=year,
            month=month,
            total_expense=Money(expense),
            total_income=Money(income),
            by_category=by_category,
        )
        return _record("bills", f"{user_id}:{prefix}", summary)

    def daily_transfer_total(self, user_id: str, on: date) -> Money:
        """日累计转出额（PDP 的三色分界输入，PRD §5.2）。"""
        rows = self._store.query(
            "SELECT COALESCE(SUM(-amount_cents), 0) AS total FROM transactions"
            " WHERE user_id = ? AND category = '转账' AND direction = 'out' AND booked_on = ?",
            (user_id, on.isoformat()),
        )
        return Money(int(rows[0]["total"]))

    def daily_transfer_limit(self, user_id: str) -> Money:
        """**客户级日限额**（银行侧口径）：按客户资产分档下发，因人而异。

        只返回结论性限额，不外露分档规则。**是否放宽由 PDP 决定**：
        官方口径不可放宽，有效阈值恒取 `min(官方 1000 元, 本限额)`。
        """
        assets = sum(record.payload.balance.cents for record in self.accounts_of(user_id))
        return Money(_asset_band_limit_cents(assets))

    def account(self, account_id: str) -> Record[Account]:
        return self._account(self._one("accounts", "account_id", account_id))

    def payee(self, payee_id: str) -> Record[Payee]:
        return self._payee(self._one("payees", "payee_id", payee_id))

    def card(self, card_id: str) -> Record[Card]:
        return self._card(self._one("cards", "card_id", card_id))

    def transaction(self, txn_id: str) -> Record[Transaction]:
        return self._transaction(self._one("transactions", "txn_id", txn_id))

    def subscription(self, subscription_id: str) -> Record[Subscription]:
        return self._subscription(self._one("subscriptions", "subscription_id", subscription_id))

    def wealth_product(self, product_id: str) -> Record[WealthProduct]:
        return self._wealth_product(self._one("wealth_products", "product_id", product_id))

    def hold(self, hold_id: str) -> Record[Hold]:
        return self._hold(self._one("holds", "hold_id", hold_id))

    def collection_request(self, request_id: str) -> Record[CollectionRequest]:
        """按主键回读一条 AA 收款请求（转账线独立表；出处校验用）。"""
        return self._collection_request(
            self._one("collection_requests", "request_id", request_id)
        )

    def scheduled_transfers(self) -> tuple[Record[ScheduledTransfer], ...]:
        """全部定时转账登记（按 `schedule_id` 升序，确定性；供到期派发读取）。"""
        rows = self._store.query("SELECT * FROM scheduled_transfers ORDER BY schedule_id")
        return tuple(self._scheduled_transfer(row) for row in rows)

    def scheduled_transfer(self, schedule_id: str) -> Record[ScheduledTransfer]:
        """按主键回读一条定时转账登记（转账线独立表；出处校验用）。"""
        return self._scheduled_transfer(
            self._one("scheduled_transfers", "schedule_id", schedule_id)
        )

    def verify(self, record_id: str, content_hash: str) -> bool:
        """出处校验：按 `record_id` 回读事实并比对哈希（**比哈希而非比文本**）。"""
        table, _, pk = record_id.partition(":")
        if not pk:
            return False
        try:
            current = self._hash_by_pk(table, pk)
        except GatewayNotFoundError:
            return False
        return current == content_hash

    def _hash_by_pk(self, table: str, pk: str) -> str:
        if table == "accounts":
            return self.account(pk).content_hash
        if table == "payees":
            return self.payee(pk).content_hash
        if table == "cards":
            return self.card(pk).content_hash
        if table == "transactions":
            return self.transaction(pk).content_hash
        if table == "subscriptions":
            return self.subscription(pk).content_hash
        if table == "wealth_products":
            return self.wealth_product(pk).content_hash
        if table == "holds":
            return self.hold(pk).content_hash
        if table == "collection_requests":
            return self.collection_request(pk).content_hash
        if table == "scheduled_transfers":
            return self.scheduled_transfer(pk).content_hash
        raise GatewayNotFoundError(f"未知记录表：{table}")

    def _one(self, table: str, key: str, value: str) -> sqlite3.Row:
        """按主键取一行。`table` / `key` **只允许内部字面量**，绝不来自用户输入。"""
        sql = f"SELECT * FROM {table} WHERE {key} = ?"
        rows = self._store.query(sql, (value,))
        if not rows:
            raise GatewayNotFoundError(f"{table} 中不存在 {key}={value}")
        return rows[0]

    def _account(self, row: object) -> Record[Account]:
        payload = Account(
            account_id=str(row["account_id"]),  # type: ignore[index]
            user_id=str(row["user_id"]),  # type: ignore[index]
            kind=str(row["kind"]),  # type: ignore[index]
            balance=Money(int(row["balance_cents"])),  # type: ignore[index]
            currency=str(row["currency"]),  # type: ignore[index]
        )
        return _record("accounts", payload.account_id, payload)

    def _payee(self, row: object) -> Record[Payee]:
        payload = Payee(
            payee_id=str(row["payee_id"]),  # type: ignore[index]
            user_id=str(row["user_id"]),  # type: ignore[index]
            full_name=str(row["full_name"]),  # type: ignore[index]
            account_tail=str(row["account_tail"]),  # type: ignore[index]
            phone_tail=str(row["phone_tail"]),  # type: ignore[index]
            identity_confirmed=bool(row["identity_confirmed"]),  # type: ignore[index]
            blocked=bool(row["blocked"]),  # type: ignore[index]
        )
        return _record("payees", payload.payee_id, payload)

    def _card(self, row: object) -> Record[Card]:
        payload = Card(
            card_id=str(row["card_id"]),  # type: ignore[index]
            user_id=str(row["user_id"]),  # type: ignore[index]
            card_type=str(row["card_type"]),  # type: ignore[index]
            status=str(row["status"]),  # type: ignore[index]
            daily_limit=Money(int(row["daily_limit_cents"])),  # type: ignore[index]
        )
        return _record("cards", payload.card_id, payload)

    def _transaction(self, row: object) -> Record[Transaction]:
        payload = Transaction(
            txn_id=str(row["txn_id"]),  # type: ignore[index]
            user_id=str(row["user_id"]),  # type: ignore[index]
            account_id=str(row["account_id"]),  # type: ignore[index]
            booked_on=date.fromisoformat(str(row["booked_on"])),  # type: ignore[index]
            amount=Money(int(row["amount_cents"])),  # type: ignore[index]
            merchant=str(row["merchant"]),  # type: ignore[index]
            category=str(row["category"]),  # type: ignore[index]
            channel=str(row["channel"]),  # type: ignore[index]
            direction=str(row["direction"]),  # type: ignore[index]
        )
        return _record("transactions", payload.txn_id, payload)

    def _subscription(self, row: object) -> Record[Subscription]:
        payload = Subscription(
            subscription_id=str(row["subscription_id"]),  # type: ignore[index]
            user_id=str(row["user_id"]),  # type: ignore[index]
            merchant=str(row["merchant"]),  # type: ignore[index]
            amount=Money(int(row["amount_cents"])),  # type: ignore[index]
            period=str(row["period"]),  # type: ignore[index]
            next_charge_on=date.fromisoformat(str(row["next_charge_on"])),  # type: ignore[index]
            status=str(row["status"]),  # type: ignore[index]
        )
        return _record("subscriptions", payload.subscription_id, payload)

    def _wealth_product(self, row: object) -> Record[WealthProduct]:
        payload = WealthProduct(
            product_id=str(row["product_id"]),  # type: ignore[index]
            name=str(row["name"]),  # type: ignore[index]
            risk_level=str(row["risk_level"]),  # type: ignore[index]
            term_days=int(row["term_days"]),  # type: ignore[index]
            yield_bp=int(row["yield_bp"]),  # type: ignore[index]
            min_amount=Money(int(row["min_amount_cents"])),  # type: ignore[index]
        )
        return _record("wealth_products", payload.product_id, payload)

    def _hold(self, row: object) -> Record[Hold]:
        payload = Hold(
            hold_id=str(row["hold_id"]),  # type: ignore[index]
            account_id=str(row["account_id"]),  # type: ignore[index]
            amount=Money(int(row["amount_cents"])),  # type: ignore[index]
            reason=str(row["reason"]),  # type: ignore[index]
            status=str(row["status"]),  # type: ignore[index]
        )
        return _record("holds", payload.hold_id, payload)

    def _collection_request(self, row: object) -> Record[CollectionRequest]:
        payload = CollectionRequest(
            request_id=str(row["request_id"]),  # type: ignore[index]
            user_id=str(row["user_id"]),  # type: ignore[index]
            payer_id=str(row["payer_id"]),  # type: ignore[index]
            amount=Money(int(row["amount_cents"])),  # type: ignore[index]
            note=str(row["note"]),  # type: ignore[index]
            status=str(row["status"]),  # type: ignore[index]
            created_on=date.fromisoformat(str(row["created_on"])),  # type: ignore[index]
        )
        return _record("collection_requests", payload.request_id, payload)

    def _scheduled_transfer(self, row: object) -> Record[ScheduledTransfer]:
        payload = ScheduledTransfer(
            schedule_id=str(row["schedule_id"]),  # type: ignore[index]
            user_id=str(row["user_id"]),  # type: ignore[index]
            account_id=str(row["account_id"]),  # type: ignore[index]
            payee_id=str(row["payee_id"]),  # type: ignore[index]
            amount=Money(int(row["amount_cents"])),  # type: ignore[index]
            booked_on=date.fromisoformat(str(row["booked_on"])),  # type: ignore[index]
            remark=str(row["remark"]),  # type: ignore[index]
            status=str(row["status"]),  # type: ignore[index]
        )
        return _record("scheduled_transfers", payload.schedule_id, payload)


def _record(table: str, pk: str, payload: T) -> Record[T]:
    return Record(
        record_id=f"{table}:{pk}",
        payload=payload,
        content_hash=sha256_hex(canonical_json(payload)),
    )
