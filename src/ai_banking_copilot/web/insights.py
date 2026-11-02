"""只读主动洞察。

洞察是演示层的提醒，不是执行授权：事实只从每次新建的只读
``FactGateway`` 读取，元数据（偏好、收件箱、扫描状态）写入独立存储。
服务不启动线程，也不在扫描时调用模型。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

from ai_banking_copilot.gateway.facts import FactGateway, Record
from ai_banking_copilot.gateway.models import Subscription, Transaction

RULE_SUBSCRIPTION = "subscription-renewal"
RULE_LARGE_EXPENSE = "large-expense-review"
RULE_BUDGET = "monthly-budget"
RULE_VERSION = "v1"
LARGE_EXPENSE_THRESHOLD_CENTS = 100_000
LOOKAHEAD_DAYS = 7
RECENT_EXPENSE_DAYS = 7


@dataclass(frozen=True)
class SourceRef:
    """事实出处；``content_hash`` 用于展示前回读校验。"""

    record_id: str
    content_hash: str


@dataclass(frozen=True)
class Insight:
    """一条可去重的站内洞察。``value`` 只承载事实或算子结果。"""

    insight_id: str
    user_id: str
    rule_id: str
    rule_version: str
    as_of: date
    window_start: date
    window_end: date
    object_id: str
    business_day: date
    source_refs: tuple[SourceRef, ...]
    formula: str
    value: object
    suggestion: str
    status: str = "active"
    updated_at: str = ""

    @property
    def key(self) -> str:
        """按用户、规则、业务对象、业务日稳定去重。"""

        return "|".join(
            (
                self.user_id,
                self.rule_id,
                self.object_id,
                self.business_day.isoformat(),
            )
        )

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["window_start"] = self.window_start.isoformat()
        data["window_end"] = self.window_end.isoformat()
        data["business_day"] = self.business_day.isoformat()
        data["source_refs"] = [asdict(item) for item in self.source_refs]
        return cast(dict[str, object], data)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Insight:
        refs = tuple(
            SourceRef(record_id=str(item["record_id"]), content_hash=str(item["content_hash"]))
            for item in cast(list[dict[str, object]], data["source_refs"])
        )
        return cls(
            insight_id=str(data["insight_id"]),
            user_id=str(data["user_id"]),
            rule_id=str(data["rule_id"]),
            rule_version=str(data["rule_version"]),
            as_of=date.fromisoformat(str(data["as_of"])),
            window_start=date.fromisoformat(str(data["window_start"])),
            window_end=date.fromisoformat(str(data["window_end"])),
            object_id=str(data["object_id"]),
            business_day=date.fromisoformat(str(data["business_day"])),
            source_refs=refs,
            formula=str(data["formula"]),
            value=data.get("value"),
            suggestion=str(data["suggestion"]),
            status=str(data.get("status", "active")),
            updated_at=str(data.get("updated_at", "")),
        )


@dataclass(frozen=True)
class ScanResult:
    user_id: str
    as_of: date
    insights: tuple[Insight, ...]
    scanned_at: str
    scan_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.scan_error is None


@dataclass(frozen=True)
class ScanState:
    user_id: str
    scanned_at: str
    error: str | None


class ReadOnlyGatewayFactory(Protocol):
    """每次调用均应创建并关闭一个独立只读连接。"""

    def __call__(self) -> AbstractContextManager[FactGateway]: ...


class InsightMetadataStore(Protocol):
    """洞察元数据存储；实现不得使用银行业务表。"""

    def get_budget(self, user_id: str, month: str) -> int | None: ...

    def set_budget(self, user_id: str, month: str, budget_cents: int | None) -> None: ...

    def get_category_budgets(self, user_id: str, month: str) -> dict[str, int]: ...

    def set_category_budgets(
        self, user_id: str, month: str, budgets: dict[str, int]
    ) -> None: ...

    def upsert_insight(self, insight: Insight) -> Insight: ...

    def list_insights(
        self, user_id: str, *, include_resolved: bool = False
    ) -> tuple[Insight, ...]: ...

    def resolve_missing(self, user_id: str, active_keys: set[str], updated_at: str) -> None: ...

    def save_scan_state(self, state: ScanState) -> None: ...

    def scan_state(self, user_id: str) -> ScanState | None: ...


class MemoryInsightMetadataStore:
    """测试和单进程演示用的独立内存元数据存储。"""

    def __init__(self) -> None:
        self._budgets: dict[tuple[str, str], int] = {}
        self._category_budgets: dict[tuple[str, str], dict[str, int]] = {}
        self._insights: dict[str, Insight] = {}
        self._scan_states: dict[str, ScanState] = {}

    def get_budget(self, user_id: str, month: str) -> int | None:
        return self._budgets.get((user_id, month))

    def set_budget(self, user_id: str, month: str, budget_cents: int | None) -> None:
        key = (user_id, month)
        if budget_cents is None:
            self._budgets.pop(key, None)
            return
        if isinstance(budget_cents, bool) or budget_cents < 0:
            raise ValueError("月预算必须是非负整数分")
        self._budgets[key] = budget_cents

    def get_category_budgets(self, user_id: str, month: str) -> dict[str, int]:
        return dict(self._category_budgets.get((user_id, month), {}))

    def set_category_budgets(
        self, user_id: str, month: str, budgets: dict[str, int]
    ) -> None:
        for category, amount in budgets.items():
            if not category or isinstance(amount, bool) or amount < 0:
                raise ValueError("分类预算必须是非负整数分")
        self._category_budgets[(user_id, month)] = dict(budgets)

    def upsert_insight(self, insight: Insight) -> Insight:
        self._insights[insight.key] = insight
        return insight

    def list_insights(self, user_id: str, *, include_resolved: bool = False) -> tuple[Insight, ...]:
        items = [item for item in self._insights.values() if item.user_id == user_id]
        if not include_resolved:
            items = [item for item in items if item.status in {"active", "needs_setup"}]
        return tuple(
            sorted(items, key=lambda item: (item.business_day, item.rule_id, item.object_id))
        )

    def resolve_missing(self, user_id: str, active_keys: set[str], updated_at: str) -> None:
        for key, item in tuple(self._insights.items()):
            if (
                item.user_id == user_id
                and item.status in {"active", "needs_setup"}
                and key not in active_keys
            ):
                self._insights[key] = replace(item, status="resolved", updated_at=updated_at)

    def save_scan_state(self, state: ScanState) -> None:
        self._scan_states[state.user_id] = state

    def scan_state(self, user_id: str) -> ScanState | None:
        return self._scan_states.get(user_id)


class SQLiteInsightMetadataStore:
    """独立 SQLite 元数据存储；数据库文件与银行事实库分离。"""

    _DDL = """
    CREATE TABLE IF NOT EXISTS insight_preferences (
        user_id TEXT NOT NULL,
        month TEXT NOT NULL,
        budget_cents INTEGER,
        category_budgets_json TEXT,
        PRIMARY KEY (user_id, month)
    );
    CREATE TABLE IF NOT EXISTS insights (
        insight_key TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        status TEXT NOT NULL,
        payload_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS insight_scan_state (
        user_id TEXT PRIMARY KEY,
        scanned_at TEXT NOT NULL,
        error TEXT
    );
    """

    def __init__(self, path: str | Path) -> None:
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(self._DDL)
        # 旧库迁移：v2 起增加分类预算列；列已存在时忽略。
        try:
            self._conn.execute(
                "ALTER TABLE insight_preferences ADD COLUMN category_budgets_json TEXT"
            )
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    def get_budget(self, user_id: str, month: str) -> int | None:
        row = self._conn.execute(
            "SELECT budget_cents FROM insight_preferences WHERE user_id = ? AND month = ?",
            (user_id, month),
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def set_budget(self, user_id: str, month: str, budget_cents: int | None) -> None:
        if budget_cents is not None and (isinstance(budget_cents, bool) or budget_cents < 0):
            raise ValueError("月预算必须是非负整数分")
        self._conn.execute(
            "INSERT INTO insight_preferences(user_id, month, budget_cents) VALUES(?, ?, ?)"
            " ON CONFLICT(user_id, month) DO UPDATE SET budget_cents = excluded.budget_cents",
            (user_id, month, budget_cents),
        )
        self._conn.commit()

    def get_category_budgets(self, user_id: str, month: str) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT category_budgets_json FROM insight_preferences"
            " WHERE user_id = ? AND month = ?",
            (user_id, month),
        ).fetchone()
        if row is None or not row[0]:
            return {}
        try:
            raw = json.loads(str(row[0]))
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(category): int(amount)
            for category, amount in raw.items()
            if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0
        }

    def set_category_budgets(
        self, user_id: str, month: str, budgets: dict[str, int]
    ) -> None:
        for category, amount in budgets.items():
            if not category or isinstance(amount, bool) or amount < 0:
                raise ValueError("分类预算必须是非负整数分")
        self._conn.execute(
            "INSERT INTO insight_preferences(user_id, month, category_budgets_json)"
            " VALUES(?, ?, ?)"
            " ON CONFLICT(user_id, month) DO UPDATE SET"
            " category_budgets_json = excluded.category_budgets_json",
            (user_id, month, json.dumps(budgets, ensure_ascii=False, sort_keys=True)),
        )
        self._conn.commit()

    def upsert_insight(self, insight: Insight) -> Insight:
        payload = json.dumps(insight.to_dict(), ensure_ascii=False, sort_keys=True)
        self._conn.execute(
            "INSERT INTO insights(insight_key, user_id, status, payload_json) VALUES(?, ?, ?, ?)"
            " ON CONFLICT(insight_key) DO UPDATE SET status = excluded.status,"
            " payload_json = excluded.payload_json",
            (insight.key, insight.user_id, insight.status, payload),
        )
        self._conn.commit()
        return insight

    def list_insights(self, user_id: str, *, include_resolved: bool = False) -> tuple[Insight, ...]:
        sql = "SELECT payload_json FROM insights WHERE user_id = ?"
        params: tuple[object, ...] = (user_id,)
        if not include_resolved:
            sql += " AND status IN ('active', 'needs_setup')"
        rows = self._conn.execute(sql, params).fetchall()
        items = tuple(
            Insight.from_dict(cast(dict[str, object], json.loads(row[0]))) for row in rows
        )
        return tuple(
            sorted(items, key=lambda item: (item.business_day, item.rule_id, item.object_id))
        )

    def resolve_missing(self, user_id: str, active_keys: set[str], updated_at: str) -> None:
        items = self.list_insights(user_id, include_resolved=True)
        for item in items:
            if item.status in {"active", "needs_setup"} and item.key not in active_keys:
                self.upsert_insight(
                    replace(item, status="resolved", updated_at=updated_at)
                )

    def save_scan_state(self, state: ScanState) -> None:
        self._conn.execute(
            "INSERT INTO insight_scan_state(user_id, scanned_at, error) VALUES(?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET scanned_at = excluded.scanned_at,"
            " error = excluded.error",
            (state.user_id, state.scanned_at, state.error),
        )
        self._conn.commit()

    def scan_state(self, user_id: str) -> ScanState | None:
        row = self._conn.execute(
            "SELECT scanned_at, error FROM insight_scan_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return ScanState(user_id=user_id, scanned_at=str(row[0]), error=row[1])

    def close(self) -> None:
        self._conn.close()


class InsightService:
    """按三条冻结规则生成只读洞察，并维护站内收件箱。"""

    def __init__(
        self,
        gateway_factory: ReadOnlyGatewayFactory,
        metadata: InsightMetadataStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._gateway_factory = gateway_factory
        self._metadata = metadata
        self._clock = clock or (lambda: datetime.now(UTC))

    def set_budget(self, user_id: str, month: str, budget_cents: int | None) -> None:
        """写入提醒偏好；该值不进入银行事实，也不改变执行授权。"""

        self._metadata.set_budget(user_id, month, budget_cents)

    def budgets(self, user_id: str, month: str) -> tuple[int | None, dict[str, int]]:
        """读取总额与分类预算；未设置时返回 ``(None, {})``。"""
        return (
            self._metadata.get_budget(user_id, month),
            self._metadata.get_category_budgets(user_id, month),
        )

    def set_budgets(
        self,
        user_id: str,
        month: str,
        budget_cents: int | None,
        categories: dict[str, int],
    ) -> None:
        """一次写入总额与分类预算；分类为空表示清空分类预算。"""
        self._metadata.set_budget(user_id, month, budget_cents)
        self._metadata.set_category_budgets(user_id, month, categories)

    def list_inbox(self, user_id: str, *, include_resolved: bool = False) -> tuple[Insight, ...]:
        return self._metadata.list_insights(user_id, include_resolved=include_resolved)

    def scan(self, user_id: str, as_of: date) -> ScanResult:
        scanned_at = self._clock().astimezone(UTC).isoformat()
        try:
            with self._gateway_factory() as gateway:
                insights = self._scan_gateway(gateway, user_id, as_of, scanned_at)
        except Exception as error:  # noqa: BLE001 - 扫描失败必须保持无副作用并可显示
            message = str(error) or error.__class__.__name__
            self._metadata.save_scan_state(ScanState(user_id, scanned_at, message))
            return ScanResult(user_id, as_of, (), scanned_at, message)

        active_keys = {item.key for item in insights}
        self._metadata.resolve_missing(user_id, active_keys, scanned_at)
        for item in insights:
            self._metadata.upsert_insight(item)
        self._metadata.save_scan_state(ScanState(user_id, scanned_at, None))
        return ScanResult(user_id, as_of, tuple(insights), scanned_at)

    def scan_state(self, user_id: str) -> ScanState | None:
        return self._metadata.scan_state(user_id)

    def _scan_gateway(
        self, gateway: FactGateway, user_id: str, as_of: date, updated_at: str
    ) -> list[Insight]:
        results: list[Insight] = []
        subscriptions = gateway.subscriptions_of(user_id)
        for record in subscriptions:
            subscription = record.payload
            days = (subscription.next_charge_on - as_of).days
            if subscription.status == "active" and 0 <= days <= LOOKAHEAD_DAYS:
                results.append(self._subscription_insight(record, as_of, days, updated_at))

        transactions = self._transactions(
            gateway, user_id, as_of - _days(RECENT_EXPENSE_DAYS - 1), as_of
        )
        for transaction_record in transactions:
            transaction = transaction_record.payload
            if transaction.amount.cents <= -LARGE_EXPENSE_THRESHOLD_CENTS:
                results.append(
                    self._large_expense_insight(transaction_record, as_of, updated_at)
                )

        month = as_of.strftime("%Y-%m")
        budget = self._metadata.get_budget(user_id, month)
        month_start = date(as_of.year, as_of.month, 1)
        month_transactions = self._transactions(gateway, user_id, month_start, as_of)
        expense = sum(
            (
                -record.payload.amount.cents
                for record in month_transactions
                if record.payload.amount.cents < 0
            ),
            0,
        )
        if budget is None:
            results.append(
                self._budget_setup_insight(user_id, as_of, month_start, expense, month, updated_at)
            )
        elif expense > budget:
            results.append(
                self._budget_insight(
                    user_id, as_of, month_start, expense, budget, month_transactions, updated_at
                )
            )
        return results

    @staticmethod
    def _transactions(
        gateway: FactGateway, user_id: str, start: date, end: date
    ) -> tuple[Record[Transaction], ...]:
        records: list[Record[Transaction]] = []
        for account in gateway.accounts_of(user_id):
            records.extend(gateway.transactions_between(account.payload.account_id, start, end))
        return tuple(sorted(records, key=lambda item: (item.payload.booked_on, item.record_id)))

    @staticmethod
    def _subscription_insight(
        record: Record[Subscription], as_of: date, days: int, updated_at: str
    ) -> Insight:
        subscription = record.payload
        return _insight(
            user_id=subscription.user_id,
            rule_id=RULE_SUBSCRIPTION,
            as_of=as_of,
            window_start=as_of,
            window_end=as_of + _days(LOOKAHEAD_DAYS),
            object_id=subscription.subscription_id,
            business_day=subscription.next_charge_on,
            source_refs=(SourceRef(record.record_id, record.content_hash),),
            formula=(
                f"days_until(next_charge_on={subscription.next_charge_on.isoformat()}, "
                f"as_of={as_of.isoformat()})={days}"
            ),
            value={"amount_cents": subscription.amount.cents, "days_remaining": days},
            suggestion=(
                f"{subscription.merchant} 将在 {subscription.next_charge_on.isoformat()} "
                "自动续费，建议确认是否继续。"
            ),
            updated_at=updated_at,
        )

    @staticmethod
    def _large_expense_insight(
        record: Record[Transaction], as_of: date, updated_at: str
    ) -> Insight:
        transaction = record.payload
        amount = -transaction.amount.cents
        return _insight(
            user_id=transaction.user_id,
            rule_id=RULE_LARGE_EXPENSE,
            as_of=as_of,
            window_start=as_of - _days(RECENT_EXPENSE_DAYS - 1),
            window_end=as_of,
            object_id=transaction.txn_id,
            business_day=transaction.booked_on,
            source_refs=(SourceRef(record.record_id, record.content_hash),),
            formula=f"expense_cents=-amount_cents({transaction.amount.cents})={amount}",
            value={"amount_cents": amount, "merchant": transaction.merchant},
            suggestion="这笔支出达到大额待核对阈值，请回读来源后再决定是否处理。",
            updated_at=updated_at,
        )

    @staticmethod
    def _budget_setup_insight(
        user_id: str,
        as_of: date,
        month_start: date,
        expense: int,
        month: str,
        updated_at: str,
    ) -> Insight:
        return _insight(
            user_id=user_id,
            rule_id=RULE_BUDGET,
            as_of=as_of,
            window_start=month_start,
            window_end=as_of,
            object_id=f"budget:{month}",
            business_day=month_start,
            source_refs=(),
            formula=f"monthly_expense_cents={expense}; budget_cents=unset",
            value={"expense_cents": expense, "budget_cents": None},
            suggestion="尚未设置本月提醒预算；设置后才会判断是否超预算。",
            status="needs_setup",
            updated_at=updated_at,
        )

    @staticmethod
    def _budget_insight(
        user_id: str,
        as_of: date,
        month_start: date,
        expense: int,
        budget: int,
        records: tuple[Record[Transaction], ...],
        updated_at: str,
    ) -> Insight:
        month = as_of.strftime("%Y-%m")
        return _insight(
            user_id=user_id,
            rule_id=RULE_BUDGET,
            as_of=as_of,
            window_start=month_start,
            window_end=as_of,
            object_id=f"budget:{month}",
            business_day=month_start,
            source_refs=tuple(SourceRef(item.record_id, item.content_hash) for item in records),
            formula=f"sum(-amount_cents where amount_cents<0)={expense} > budget_cents={budget}",
            value={
                "expense_cents": expense,
                "budget_cents": budget,
                "over_cents": expense - budget,
            },
            suggestion="本月支出已超过提醒预算，请查看明细并自行决定后续操作。",
            updated_at=updated_at,
        )


def _insight(
    *,
    user_id: str,
    rule_id: str,
    as_of: date,
    window_start: date,
    window_end: date,
    object_id: str,
    business_day: date,
    source_refs: tuple[SourceRef, ...],
    formula: str,
    value: object,
    suggestion: str,
    updated_at: str,
    status: str = "active",
) -> Insight:
    key = "|".join((user_id, rule_id, object_id, business_day.isoformat()))
    insight_id = sha256(key.encode("utf-8")).hexdigest()[:24]
    return Insight(
        insight_id=insight_id,
        user_id=user_id,
        rule_id=rule_id,
        rule_version=RULE_VERSION,
        as_of=as_of,
        window_start=window_start,
        window_end=window_end,
        object_id=object_id,
        business_day=business_day,
        source_refs=source_refs,
        formula=formula,
        value=value,
        suggestion=suggestion,
        status=status,
        updated_at=updated_at,
    )


def _days(value: int) -> timedelta:
    return timedelta(days=value)
