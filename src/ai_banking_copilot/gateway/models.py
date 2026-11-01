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
    phone: str | None = None
    """名册登记的完整 11 位手机号（可空 = 关系未知）。

    只有非空且**逐位相等**才允许用完整手机号命中；`None` 时不得退回尾号匹配——
    否则两个不同号码只要后四位相同就会指向同一个人。
    """


@dataclass(frozen=True)
class Card:
    card_id: str
    user_id: str
    card_type: str
    status: str
    daily_limit: Money
    account_id: str | None = None
    """绑定的结算账户（可空 = 关系未知；不得据此推断"无账户"）。"""
    last4: str | None = None
    """演示卡号后四位：**不是真实卡号**，仅用于界面显示「尾号 9021」；旧库为 NULL。"""
    brand: str | None = None
    """卡组织（unionpay / mastercard / visa）；NULL = 未知，不猜测。"""
    expiry_month: int | None = None
    expiry_year: int | None = None
    """演示有效期；NULL = 未知。"""
    account_class: str | None = None
    """借记卡账户类别（Ⅰ / Ⅱ / Ⅲ 类户）；信用卡为 NULL。"""
    atm_limit: Money | None = None
    transfer_limit: Money | None = None
    purchase_limit: Money | None = None
    overseas_limit: Money | None = None
    """借记卡四通道日限额；NULL = 该通道未配置，不等于 0。"""
    credit_limit: Money | None = None
    temp_credit_limit: Money | None = None
    temp_credit_until: str | None = None
    cycle_limit: Money | None = None
    """信用卡固定额度 / 生效中的临时额度 / 账单周期消费限额；NULL = 不适用或未知。"""
    temp_lock_until: str | None = None
    lock_reason: str | None = None
    """临时冻结（口头挂失）到期日与原因；`status=locked` 时才有意义。"""
    lost_at: str | None = None
    loss_reason: str | None = None
    """正式挂失时间与原因；`status=lost` 时才有意义。"""
    replaced_by_card_id: str | None = None
    """补卡后的新卡 id；非空表示旧卡已作废、不可再补。"""


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
    booked_at: str | None = None
    """发生时刻（`YYYY-MM-DDTHH:MM:SS`，可空）。「深夜」判据的唯一时间来源。"""
    city: str | None = None
    """消费发生地城市（可空）。「异地」判据的唯一地点来源。"""
    card_tail: str | None = None
    """付款卡尾号（可空）；缺失时不得从账户或商户推断。"""


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
class WealthHolding:
    """理财持仓（只读事实）：金额与申购/赎回同源，`purchased_on` 为起息日。"""

    product_id: str
    user_id: str
    amount: Money
    purchased_on: date


@dataclass(frozen=True)
class WealthNav:
    """单只产品的单日净值。

    `nav_bp` 为净值 × 10000（10000 = 净值 1.0000）。**演示用模拟序列**，非真实净值；
    由 `gateway/seed.py` 的确定性公式生成，不接任何行情源。
    """

    product_id: str
    on_date: date
    nav_bp: int


@dataclass(frozen=True)
class WealthDisclosure:
    """理财销售披露事实（与 `WealthProduct` 分离，避免改核心产品哈希）。

    费率单位是基点（1 bp = 0.01%）；`None` = 未披露，**不得**按 0 处理。
    """

    product_id: str
    product_type: str
    registration_code: str
    issuer: str
    issuer_risk_level: str
    seller_risk_level: str
    subscription_fee_bp: int | None
    service_fee_bp: int | None
    custody_fee_bp: int | None
    increment_cents: int
    redeem_mode: str
    fast_redeem_limit_cents: int | None
    closed_days: int
    next_open_date: date | None
    purchase_cutoff: str
    sale_status: str
    updated_on: date

    @property
    def display_risk_level(self) -> str:
        """发行方与销售方评级取**孰高**（《适当性管理办法》第二十二条）。"""
        order = {"R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}
        return max(
            (self.issuer_risk_level, self.seller_risk_level),
            key=lambda item: order.get(item, 0),
        )

    @property
    def disclosure_complete(self) -> bool:
        """任一必填披露缺失 ⇒ 不可售（缺数据不上架）。"""
        return (
            self.sale_status == "on_sale"
            and bool(self.registration_code)
            and self.subscription_fee_bp is not None
            and self.service_fee_bp is not None
            and self.custody_fee_bp is not None
            and self.increment_cents > 0
            and self.redeem_mode in {"realtime", "t_plus_1", "t_plus_3", "closed"}
        )


@dataclass(frozen=True)
class RiskAssessmentEvent:
    """一次**已提交**的风险测评（用于 12 个月累计与结果变更判断）。"""

    event_id: str
    user_id: str
    event_on: date
    risk_profile: str
    score: int
    source: str
    previous_profile: str | None = None


@dataclass(frozen=True)
class WealthTradeAck:
    """交易确认留痕：四件套阅读、抄录哈希、分红方式与偏好。"""

    ack_id: str
    user_id: str
    product_id: str
    action: str
    doc_versions: dict[str, str]
    read_seconds: dict[str, int]
    declaration_text: str | None
    declaration_hash: str | None
    dividend_method: str | None
    auto_redeem_pref: bool | None
    account_id: str | None
    created_at: str


@dataclass(frozen=True)
class CustomerSuitabilityProfile:
    """客户适当性属性：目前只有出生年份（用于 65+ 特别提示）。"""

    user_id: str
    birth_year: int | None = None


@dataclass(frozen=True)
class Subscription:
    subscription_id: str
    user_id: str
    merchant: str
    amount: Money
    period: str
    next_charge_on: date
    status: str
    card_id: str | None = None
    """代扣绑定的卡（可空 = 关系未知；不得据此推断"不限卡"）。"""


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
