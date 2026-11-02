"""统一流水读模型：页面与场景只从这里读取交易事实。

边界：
- 只读 `FactGateway`，不写账本、不生成事实；
- `record_no` 是展示编号（`txn_id`，已是 `txn_` 开头），`record_id` 只用于出处校验；
- 金额保持带符号整数分：支出为负、收入/冲正为正；
- 「代扣」视图只按 `type` 过滤，不另存一份扣款数据；
- 付款账户只陈述流水事实：交易带 `card_tail` 才显示卡尾号，缺失时显示账户名，
  **不从账户或商户推断卡号**（与 `gateway.models.Transaction.card_tail` 的约定一致）。
"""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Account, Card, Transaction
from ai_banking_copilot.scenarios.bill_categories import (
    display_merchant,
    is_consumption_category,
    normalize_merchant,
)

CARD_TYPE_LABELS = {"debit": "借记卡", "credit": "信用卡", "virtual": "虚拟卡"}
ACCOUNT_KIND_LABELS = {"current": "活期结算账户", "savings": "储蓄存款账户"}

TYPE_DIRECT_DEBIT = "direct-debit"
TYPE_AUTO_RENEWAL = "auto-renewal"
TYPE_TRANSFER = "transfer"
TYPE_REVERSAL = "reversal"
TYPE_REFUND = "refund"
TYPE_CONSUMPTION = "consumption"
TYPE_INCOME = "income"
TYPE_LABELS = {
    TYPE_DIRECT_DEBIT: "代扣",
    TYPE_AUTO_RENEWAL: "自动续费",
    TYPE_TRANSFER: "转账",
    TYPE_REVERSAL: "退款冲正",
    TYPE_REFUND: "退款",
    TYPE_CONSUMPTION: "消费",
    TYPE_INCOME: "收入",
}

STATUS_SUCCESS = "success"
STATUS_REVERSED = "reversed"
STATUS_LABELS = {STATUS_SUCCESS: "成功", STATUS_REVERSED: "已冲正"}

DIRECTION_LABELS = {"out": "支出", "in": "收入"}

CYCLE_MONTHLY = "monthly"
CYCLE_YEARLY = "yearly"
CYCLE_UNKNOWN = "unknown"
CYCLE_LABELS = {CYCLE_MONTHLY: "月度", CYCLE_YEARLY: "年度", CYCLE_UNKNOWN: "周期未知"}
CYCLE_MONTHLY_DAYS = (25, 35)
CYCLE_YEARLY_DAYS = (350, 380)

CHARGE_WINDOW = 12
"""扣款记录默认窗口：每商户最近 12 期；全量汇总单独给。"""

_SUBSCRIPTION_KEYWORDS = ("会员", "订阅", "自动续费", "服务费", "增值服务", "vip")
_AUTO_RENEWAL_TOLERANCE_PERCENT = 5


@dataclass(frozen=True)
class PaymentInstrument:
    """一笔流水实际使用的付款账户（只陈述流水事实）。"""

    account_id: str
    account_kind: str
    account_label: str
    card_type: str | None
    card_type_label: str
    card_tail: str | None
    display_label: str
    is_settlement: bool
    warning: str = ""

    def payload(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "account_kind": self.account_kind,
            "account_label": self.account_label,
            "card_type": self.card_type,
            "card_type_label": self.card_type_label,
            "card_tail": self.card_tail,
            "display_label": self.display_label,
            "is_settlement": self.is_settlement,
            "warning": self.warning,
        }


@dataclass(frozen=True)
class LedgerEntry:
    """统一流水行；所有展示字段都由 gateway 事实派生。"""

    txn_id: str
    record_no: str
    record_id: str
    content_hash: str
    account_id: str
    booked_on: date
    booked_at: str | None
    merchant: str
    merchant_label: str
    category: str
    channel: str
    type: str
    direction: str
    amount_cents: int
    status: str
    card_tail: str | None
    instrument: PaymentInstrument

    @property
    def merchant_key(self) -> str:
        return normalize_merchant(self.merchant)

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def direction_label(self) -> str:
        return DIRECTION_LABELS.get(self.direction, self.direction)


@dataclass(frozen=True)
class ChargeGroup:
    """按商户聚合的扣款记录。"""

    merchant_key: str
    merchant: str
    subscription_id: str | None
    type: str
    cycle: str
    cycle_label: str
    window: int
    window_count: int
    window_total_cents: int
    history_count: int
    history_total_cents: int
    last_charged_on: date | None
    next_charge_on: date | None
    next_charge_basis: str
    charges: tuple[LedgerEntry, ...]

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type)


def collect_entries(
    gateway: FactGateway,
    user_id: str,
    *,
    as_of: date,
) -> tuple[LedgerEntry, ...]:
    """一次扫描该用户全部账户流水，返回带类型/状态/付款账户的统一行。"""
    accounts = {
        record.payload.account_id: record.payload
        for record in gateway.accounts_of(user_id)
    }
    cards = [record.payload for record in gateway.cards_of(user_id)]
    registered = {
        normalize_merchant(record.payload.merchant): record.payload
        for record in gateway.subscriptions_of(user_id)
        if record.payload.user_id == user_id
    }
    raw: list[tuple[Account, str, str, Transaction]] = []
    for account_id, account in accounts.items():
        for record in gateway.transactions_between(account_id, date.min, as_of):
            transaction = record.payload
            if transaction.user_id != user_id or transaction.amount.cents == 0:
                continue
            raw.append((account, record.record_id, record.content_hash, transaction))

    reversed_ids = {
        transaction.merchant.removeprefix("冲正 ").strip()
        for _account, _record_id, _hash, transaction in raw
        if transaction.category == "冲正" or transaction.merchant.startswith("冲正 ")
    }
    auto_keys = _auto_renewal_keys(raw, registered)
    entries: list[LedgerEntry] = []
    for account, record_id, content_hash, transaction in raw:
        entries.append(
            LedgerEntry(
                txn_id=transaction.txn_id,
                record_no=transaction.txn_id,
                record_id=record_id,
                content_hash=content_hash,
                account_id=account.account_id,
                booked_on=transaction.booked_on,
                booked_at=transaction.booked_at,
                merchant=transaction.merchant,
                merchant_label=display_merchant(transaction.merchant),
                category=transaction.category,
                channel=transaction.channel,
                type=_entry_type(transaction, registered, auto_keys),
                direction=transaction.direction,
                amount_cents=transaction.amount.cents,
                status=(
                    STATUS_REVERSED
                    if transaction.txn_id in reversed_ids
                    else STATUS_SUCCESS
                ),
                card_tail=transaction.card_tail,
                instrument=_instrument(accounts, cards, account.account_id, transaction.card_tail),
            )
        )
    return tuple(sorted(entries, key=_entry_sort_key))


def charge_groups(
    gateway: FactGateway,
    user_id: str,
    *,
    as_of: date,
    window: int = CHARGE_WINDOW,
) -> tuple[ChargeGroup, ...]:
    """按商户聚合「代扣/自动续费」流水；窗口与全量计数同源。"""
    entries = collect_entries(gateway, user_id, as_of=as_of)
    by_txn = {entry.txn_id: entry for entry in entries}
    groups: dict[str, list[LedgerEntry]] = defaultdict(list)
    for entry in entries:
        if entry.type in {TYPE_DIRECT_DEBIT, TYPE_AUTO_RENEWAL}:
            groups[entry.merchant_key].append(entry)
            continue
        if entry.type != TYPE_REVERSAL:
            continue
        target_id = entry.merchant.removeprefix("冲正 ").strip()
        target = by_txn.get(target_id)
        if target is None or target.type not in {TYPE_DIRECT_DEBIT, TYPE_AUTO_RENEWAL}:
            continue
        groups[target.merchant_key].append(
            replace(
                entry,
                merchant=target.merchant,
                merchant_label=target.merchant_label,
                category=target.category,
                type=TYPE_REVERSAL,
            )
        )

    subscriptions = {
        normalize_merchant(record.payload.merchant): record.payload
        for record in gateway.subscriptions_of(user_id)
        if record.payload.user_id == user_id
    }
    result: list[ChargeGroup] = []
    for key, rows in groups.items():
        ordered = tuple(sorted(rows, key=_entry_sort_key))
        debits = tuple(row for row in ordered if row.type != TYPE_REVERSAL)
        if not debits:
            continue
        window_rows = ordered[-window:] if window > 0 else ordered
        gaps = _gaps([row.booked_on for row in debits])
        cycle, median_gap = _infer_cycle(gaps)
        last_on = debits[-1].booked_on
        raw_next = (
            last_on + timedelta(days=median_gap)
            if median_gap is not None and cycle != CYCLE_UNKNOWN
            else None
        )
        next_on, _overdue = (
            _advance_until_future(raw_next, cycle, as_of) if raw_next else (None, None)
        )
        subscription = subscriptions.get(key)
        result.append(
            ChargeGroup(
                merchant_key=key,
                merchant=debits[0].merchant_label,
                subscription_id=(
                    subscription.subscription_id if subscription is not None else None
                ),
                type=(
                    TYPE_DIRECT_DEBIT if subscription is not None else debits[0].type
                ),
                cycle=cycle,
                cycle_label=CYCLE_LABELS[cycle],
                window=window,
                window_count=len(window_rows),
                window_total_cents=-sum(row.amount_cents for row in window_rows),
                history_count=len(ordered),
                history_total_cents=-sum(row.amount_cents for row in ordered),
                last_charged_on=last_on,
                next_charge_on=next_on,
                next_charge_basis=_next_charge_basis(
                    cycle=cycle,
                    median_gap=median_gap,
                    next_on=next_on,
                ),
                charges=window_rows,
            )
        )
    return tuple(
        sorted(
            result,
            key=lambda item: (
                -item.window_total_cents,
                item.last_charged_on or date.min,
                item.merchant_key,
            ),
        )
    )


def charge_summary(groups: Sequence[ChargeGroup]) -> dict[str, int]:
    """扣款视图汇总：窗口与全量分别给出，页面不得自行重算。"""
    return {
        "window": max((group.window for group in groups), default=CHARGE_WINDOW),
        "window_count": sum(group.window_count for group in groups),
        "window_total_cents": sum(group.window_total_cents for group in groups),
        "history_count": sum(group.history_count for group in groups),
        "history_total_cents": sum(group.history_total_cents for group in groups),
        "merchant_count": len(groups),
    }


def charge_payload(group: ChargeGroup) -> dict[str, object]:
    """扣款聚合的渠道序列化；前端只渲染这些字段。"""
    return {
        "merchant_key": group.merchant_key,
        "merchant": group.merchant,
        "subscription_id": group.subscription_id,
        "type": group.type,
        "type_label": group.type_label,
        "cycle": group.cycle,
        "cycle_label": group.cycle_label,
        "window": group.window,
        "window_count": group.window_count,
        "window_total_cents": group.window_total_cents,
        "history_count": group.history_count,
        "history_total_cents": group.history_total_cents,
        "last_charged_on": (
            group.last_charged_on.isoformat() if group.last_charged_on else None
        ),
        "next_charge_on": (
            group.next_charge_on.isoformat() if group.next_charge_on else None
        ),
        "next_charge_basis": group.next_charge_basis,
        "charges": [entry_payload(entry) for entry in group.charges],
    }


def entry_payload(entry: LedgerEntry) -> dict[str, object]:
    return {
        "txn_id": entry.txn_id,
        "record_no": entry.record_no,
        "record_id": entry.record_id,
        "content_hash": entry.content_hash,
        "account_id": entry.account_id,
        "booked_on": entry.booked_on.isoformat(),
        "booked_at": entry.booked_at,
        "merchant": entry.merchant,
        "merchant_label": entry.merchant_label,
        "category": entry.category,
        "channel": entry.channel,
        "type": entry.type,
        "type_label": entry.type_label,
        "direction": entry.direction,
        "direction_label": entry.direction_label,
        "amount_cents": entry.amount_cents,
        "status": entry.status,
        "status_label": entry.status_label,
        "card_tail": entry.card_tail,
        "payment_account": entry.instrument.payload(),
    }


def account_options(gateway: FactGateway, user_id: str) -> list[dict[str, object]]:
    """账户下拉的统一标签：活期账户按借记卡+尾号命名，储蓄账户不再混用「活期」。"""
    cards = [record.payload for record in gateway.cards_of(user_id)]
    result: list[dict[str, object]] = []
    for record in gateway.accounts_of(user_id):
        account = record.payload
        debit = _debit_card_for(account.account_id, cards)
        kind_label = ACCOUNT_KIND_LABELS.get(account.kind, account.kind)
        if account.kind == "current":
            label = (
                f"{_card_label(debit)} · 活期结算"
                if debit is not None
                else "活期结算账户"
            )
        elif account.kind == "savings":
            label = "储蓄存款账户"
        else:
            label = kind_label
        result.append(
            {
                "id": account.account_id,
                "kind": account.kind,
                "kind_label": kind_label,
                "label": label,
                "card_tail": debit.last4 if debit is not None else None,
                "card_type": debit.card_type if debit is not None else None,
                "card_type_label": (
                    CARD_TYPE_LABELS.get(debit.card_type, debit.card_type)
                    if debit is not None
                    else ""
                ),
                "balance_cents": account.balance.cents,
            }
        )
    return result


def payment_account_label(
    gateway: FactGateway, user_id: str, account_id: str, card_tail: str | None
) -> str:
    """单笔流水的付款账户标签；供转账记录等复用同一套命名。"""
    accounts = {
        record.payload.account_id: record.payload
        for record in gateway.accounts_of(user_id)
    }
    cards = [record.payload for record in gateway.cards_of(user_id)]
    account = accounts.get(account_id)
    if account is None:
        return "账户未知"
    return _instrument(accounts, cards, account_id, card_tail).display_label


def _instrument(
    accounts: Mapping[str, Account],
    cards: Sequence[Card],
    account_id: str,
    card_tail: str | None,
) -> PaymentInstrument:
    account = accounts.get(account_id)
    account_kind = account.kind if account is not None else ""
    account_label = ACCOUNT_KIND_LABELS.get(account_kind, account_kind or "账户未知")
    card = _card_by_tail(card_tail, cards)
    if card is None:
        return PaymentInstrument(
            account_id=account_id,
            account_kind=account_kind,
            account_label=account_label,
            card_type=None,
            card_type_label="",
            card_tail=None,
            display_label=account_label,
            is_settlement=account_kind == "current",
            warning=(
                "该笔扣款来自储蓄存款账户，不是结算账户。"
                if account_kind == "savings"
                else ""
            ),
        )
    is_settlement = account_kind == "current" and card.card_type == "debit"
    warning = ""
    if account_kind == "savings":
        warning = "该笔扣款来自储蓄存款账户，不是结算账户。"
    elif card.card_type == "credit":
        warning = "该笔扣款来自信用卡，不是借记卡结算账户。"
    return PaymentInstrument(
        account_id=account_id,
        account_kind=account_kind,
        account_label=account_label,
        card_type=card.card_type,
        card_type_label=CARD_TYPE_LABELS.get(card.card_type, card.card_type),
        card_tail=card.last4,
        display_label=_card_label(card),
        is_settlement=is_settlement,
        warning=warning,
    )


def _card_by_tail(card_tail: str | None, cards: Sequence[Card]) -> Card | None:
    if not card_tail:
        return None
    for card in cards:
        if card.last4 == card_tail:
            return card
    return None


def _debit_card_for(account_id: str, cards: Sequence[Card]) -> Card | None:
    matches = [
        card
        for card in cards
        if card.account_id == account_id and card.card_type == "debit" and card.last4
    ]
    return matches[0] if len(matches) == 1 else None


def _card_label(card: Card) -> str:
    label = CARD_TYPE_LABELS.get(card.card_type, "卡")
    return f"{label}（尾号 {card.last4}）" if card.last4 else f"{label}（尾号未提供）"


def _entry_type(
    transaction: Transaction,
    registered: Mapping[str, object],
    auto_keys: set[str],
) -> str:
    if transaction.category == "冲正" or transaction.merchant.startswith("冲正 "):
        return TYPE_REVERSAL
    if not is_consumption_category(transaction.category):
        return TYPE_TRANSFER if transaction.direction == "out" else TYPE_INCOME
    key = normalize_merchant(transaction.merchant)
    if key in registered:
        return TYPE_DIRECT_DEBIT
    if key in auto_keys:
        return TYPE_AUTO_RENEWAL
    if transaction.direction == "out":
        return TYPE_CONSUMPTION
    return TYPE_REFUND


def _auto_renewal_keys(
    raw: Sequence[tuple[Account, str, str, Transaction]],
    registered: Mapping[str, object],
) -> set[str]:
    grouped: dict[str, list[Transaction]] = defaultdict(list)
    for _account, _record_id, _hash, transaction in raw:
        if transaction.direction != "out" or not is_consumption_category(transaction.category):
            continue
        grouped[normalize_merchant(transaction.merchant)].append(transaction)
    result: set[str] = set()
    for key, unsorted in grouped.items():
        items = sorted(unsorted, key=lambda item: (item.booked_on, item.txn_id))
        if key in registered or len(items) < 3:
            continue
        if not any(keyword in key for keyword in _SUBSCRIPTION_KEYWORDS):
            continue
        cycle, _gap = _infer_cycle(_gaps([item.booked_on for item in items]))
        if cycle == CYCLE_UNKNOWN:
            continue
        amounts = [abs(item.amount.cents) for item in items[-3:]]
        median = _median_int(amounts)
        if median <= 0 or max(amounts) - min(amounts) > (
            median * _AUTO_RENEWAL_TOLERANCE_PERCENT // 100
        ):
            continue
        result.add(key)
    return result


def _infer_cycle(gaps: Sequence[int]) -> tuple[str, int | None]:
    if not gaps:
        return CYCLE_UNKNOWN, None
    median_gap = _median_int(gaps)
    if CYCLE_MONTHLY_DAYS[0] <= median_gap <= CYCLE_MONTHLY_DAYS[1]:
        return CYCLE_MONTHLY, median_gap
    if CYCLE_YEARLY_DAYS[0] <= median_gap <= CYCLE_YEARLY_DAYS[1]:
        return CYCLE_YEARLY, median_gap
    return CYCLE_UNKNOWN, median_gap


def _advance_until_future(
    day: date, cycle: str, as_of: date
) -> tuple[date, int | None]:
    if day >= as_of:
        return day, None
    cursor = day
    guard = 0
    while cursor < as_of and guard < 120:
        cursor = _add_cycle(cursor, cycle)
        guard += 1
    return cursor, max((as_of - day).days, 0)


def _add_cycle(day: date, cycle: str) -> date:
    if cycle == CYCLE_MONTHLY:
        year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
        return date(year, month, _clamp_day(year, month, day.day))
    if cycle == CYCLE_YEARLY:
        year = day.year + 1
        return date(year, day.month, _clamp_day(year, day.month, day.day))
    return day + timedelta(days=30)


def _clamp_day(year: int, month: int, day: int) -> int:
    return min(day, monthrange(year, month)[1])


def _next_charge_basis(
    *, cycle: str, median_gap: int | None, next_on: date | None
) -> str:
    if next_on is None or median_gap is None:
        return "历史扣款不足，无法推算下次扣款日；以商户实际扣款为准。"
    return (
        f"按最近扣款间隔的中位数 {median_gap} 天（{CYCLE_LABELS.get(cycle, cycle)}）推得，"
        "仅作预测；实际扣费日以商户为准。"
    )


def _gaps(dates: Sequence[date]) -> tuple[int, ...]:
    return tuple(
        (later - earlier).days for earlier, later in zip(dates, dates[1:], strict=False)
    )


def _median_int(values: Sequence[int]) -> int:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def _entry_sort_key(entry: LedgerEntry) -> tuple[date, str, str]:
    return (entry.booked_on, entry.booked_at or "", entry.txn_id)
