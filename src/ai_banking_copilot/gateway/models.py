"""领域模型：模拟核心银行的事实对象（纯数据，不含任何 I/O）。

符号约定：
- 金额一律 `Money`（单位：分）；`Transaction.amount` **带符号**（支出为负、收入为正）。
- `BillSummary.by_category` 保留**带符号**原始和；`total_expense` / `total_income` 为**正值**合计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Money:
    """以「分」为最小单位的金额——避免浮点误差，且可确定性序列化。"""

    cents: int

    def __post_init__(self) -> None:
        if isinstance(self.cents, bool) or not isinstance(self.cents, int):
            raise TypeError("Money.cents 必须是整数（单位：分）")

    def __add__(self, other: Money) -> Money:
        return Money(self.cents + other.cents)

    def __sub__(self, other: Money) -> Money:
        return Money(self.cents - other.cents)

    def __neg__(self) -> Money:
        return Money(-self.cents)

    @property
    def yuan(self) -> Decimal:
        return (Decimal(self.cents) / Decimal(100)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class Account:
    account_id: str
    user_id: str
    kind: str
    balance: Money
    currency: str = "CNY"


@dataclass(frozen=True)
class Payee:
    """名册中的收款对象。升迁唯一谓词 = `identity_confirmed`（PRD §5.2）。"""

    payee_id: str
    user_id: str
    full_name: str
    account_tail: str
    phone_tail: str
    identity_confirmed: bool = False
    blocked: bool = False


@dataclass(frozen=True)
class Card:
    card_id: str
    user_id: str
    card_type: str
    status: str
    daily_limit: Money


@dataclass(frozen=True)
class Transaction:
    txn_id: str
    user_id: str
    account_id: str
    booked_on: date
    amount: Money
    merchant: str
    category: str
    channel: str
    direction: str


@dataclass(frozen=True)
class WealthProduct:
    """理财产品。`yield_bp` 为年化基点，**仅陈述事实**，不得转写为收益承诺（H6）。"""

    product_id: str
    name: str
    risk_level: str
    term_days: int
    yield_bp: int
    min_amount: Money


@dataclass(frozen=True)
class Subscription:
    subscription_id: str
    user_id: str
    merchant: str
    amount: Money
    period: str
    next_charge_on: date
    status: str


@dataclass(frozen=True)
class Hold:
    """资金冻结（cf 口径：冻结视同流出，PRD §5.3）。"""

    hold_id: str
    account_id: str
    amount: Money
    reason: str
    status: str


@dataclass(frozen=True)
class CategoryTotal:
    category: str
    total: Money
    txn_count: int


@dataclass(frozen=True)
class BillSummary:
    user_id: str
    year: int
    month: int
    total_expense: Money
    total_income: Money
    by_category: tuple[CategoryTotal, ...] = field(default_factory=tuple)
