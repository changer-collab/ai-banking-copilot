"""生活事件的资金来源核查（确定性、只读、只提示）。

跨场景联动最有价值的一步：**在把钱花出去之前，核对那一天钱够不够**。
本模块不生成事实：可用余额来自 `accounts`，到期本金来自 `wealth_holdings` 与产品期限，
日期只与 `today` 比较；输出只用于页面黄条与对话提示，**不阻塞、不改计划、不改授权**。

口径：
- 可用余额 = 本人活期账户 `balance` 之和（冻结时已从 balance 扣减，冻结额不在其中）；
- 到期本金 = 到期日不晚于 `trigger_on` 的持仓本金之和（含**已到期未赎回**，可随时赎回；
  按产品条款：起息日 + 期限，不按流水反推）；
- `later` = 当前余额不够，但要花的那天之前有理财到期，赎回后可覆盖；
- `short` = 到期资金也不够，或最近一笔到期日**晚于**用钱日。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.scenarios.life_events import holding_maturity

STATUS_OK = "ok"
STATUS_LATER = "later"
STATUS_SHORT = "short"
STATUS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class FundingCheck:
    """一条事件的资金核查结果。"""

    status: str
    trigger_on: date | None = None
    amount_cents: int | None = None
    available_cents: int = 0
    inflow_cents: int = 0
    inflow_on: date | None = None
    gap_cents: int = 0
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "trigger_on": None if self.trigger_on is None else self.trigger_on.isoformat(),
            "amount_cents": self.amount_cents,
            "available_cents": self.available_cents,
            "inflow_cents": self.inflow_cents,
            "inflow_on": None if self.inflow_on is None else self.inflow_on.isoformat(),
            "gap_cents": self.gap_cents,
            "message": self.message,
        }

    @property
    def needs_warning(self) -> bool:
        return self.status in (STATUS_LATER, STATUS_SHORT)


def _yuan(cents: int) -> str:
    return f"{cents / 100:,.2f}"


def _maturities(
    gateway: FactGateway, user_id: str, today: date
) -> tuple[tuple[date, str, int], ...]:
    """`(到期日, 产品名, 本金分)`，按到期日升序；无期限产品不列。"""

    products = {
        record.payload.product_id: record.payload for record in gateway.wealth_products()
    }
    rows: list[tuple[date, str, int]] = []
    for record in gateway.wealth_holdings_of(user_id):
        holding = record.payload
        product = products.get(holding.product_id)
        if product is None:
            continue
        maturity = holding_maturity(holding.purchased_on, product.term_days)
        if maturity is None:
            continue
        rows.append((maturity, product.name, holding.amount.cents))
    rows.sort(key=lambda item: (item[0], item[1]))
    return tuple(rows)


def available_cents(gateway: FactGateway, user_id: str) -> int:
    """本人活期账户可用余额之和（分）。"""

    return sum(record.payload.balance.cents for record in gateway.accounts_of(user_id))


def check(
    gateway: FactGateway,
    user_id: str,
    *,
    amount_cents: int | None,
    trigger_on: date | None,
    today: date,
) -> FundingCheck:
    """核对「trigger_on 那天要花 amount」够不够；缺金额或缺日期返回 `unknown`。"""

    if amount_cents is None or amount_cents <= 0 or trigger_on is None:
        return FundingCheck(status=STATUS_UNKNOWN)
    available = available_cents(gateway, user_id)
    if amount_cents <= available:
        return FundingCheck(
            status=STATUS_OK,
            trigger_on=trigger_on,
            amount_cents=amount_cents,
            available_cents=available,
            message=f"可用余额 ¥{_yuan(available)} 可覆盖",
        )
    gap_now = amount_cents - available
    maturities = _maturities(gateway, user_id, today)
    in_window = [item for item in maturities if item[0] <= trigger_on]
    inflow = sum(item[2] for item in in_window)
    if in_window and amount_cents <= available + inflow:
        first = in_window[0]
        return FundingCheck(
            status=STATUS_LATER,
            trigger_on=trigger_on,
            amount_cents=amount_cents,
            available_cents=available,
            inflow_cents=inflow,
            inflow_on=first[0],
            gap_cents=gap_now,
            message=(
                f"当前可用 ¥{_yuan(available)}，还差 ¥{_yuan(gap_now)}；"
                f"「{first[1]}」{first[0].isoformat()} 到期赎回后可用"
            ),
        )
    later = [item for item in maturities if item[0] > trigger_on]
    when = (
        f"最近一笔「{later[0][1]}」{later[0][0].isoformat()} 才到期"
        if later
        else "没有可覆盖的到期资金"
    )
    return FundingCheck(
        status=STATUS_SHORT,
        trigger_on=trigger_on,
        amount_cents=amount_cents,
        available_cents=available,
        inflow_cents=inflow,
        inflow_on=None if not later else later[0][0],
        gap_cents=amount_cents - available - inflow,
        message=(
            f"可用余额 ¥{_yuan(available)}，距 {trigger_on.isoformat()} 尚差 "
            f"¥{_yuan(amount_cents - available - inflow)}；{when}"
        ),
    )


def note(result: FundingCheck) -> str | None:
    """对话侧一句话提醒；无需提醒返回 `None`。"""

    if not result.needs_warning or result.trigger_on is None:
        return None
    if result.status == STATUS_LATER:
        return (
            f"资金提醒：{result.trigger_on.isoformat()} 这笔要用 "
            f"¥{_yuan(result.amount_cents or 0)}，当前可用 ¥{_yuan(result.available_cents)}；"
            f"{result.message}。可以提前赎回、改日期或换资金来源。"
        )
    return (
        f"资金提醒：{result.trigger_on.isoformat()} 这笔要用 "
        f"¥{_yuan(result.amount_cents or 0)}；{result.message}。"
        "可以提前赎回、改日期或换资金来源。"
    )
