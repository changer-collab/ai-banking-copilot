"""理财产品披露事实：独立表，**不改** `wealth_products` 核心表与 `WealthProduct` 哈希。

口径：
- 费率、登记编码、流动性、发行/销售评级都属于「销售披露」，与可交易产品事实分离；
- 任一必填披露缺失 ⇒ `disclosure_complete = False`，产品目录标「暂不可售」；
- 登记编码是**演示编码**，不是官方登记编码；界面必须如实标注；
- 发行/销售评级不一致时按**孰高**展示（《金融机构产品适当性管理办法》第二十二条）。
"""

from __future__ import annotations

from datetime import date

from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.models import WealthDisclosure
from ai_banking_copilot.gateway.store import Store

WEALTH_DISCLOSURE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS wealth_disclosures (
    product_id              TEXT PRIMARY KEY REFERENCES wealth_products(product_id),
    product_type            TEXT NOT NULL,
    registration_code       TEXT NOT NULL,
    issuer                  TEXT NOT NULL,
    issuer_risk_level       TEXT NOT NULL,
    seller_risk_level       TEXT NOT NULL,
    subscription_fee_bp     INTEGER,
    service_fee_bp          INTEGER,
    custody_fee_bp          INTEGER,
    increment_cents         INTEGER NOT NULL,
    redeem_mode             TEXT NOT NULL,
    fast_redeem_limit_cents INTEGER,
    closed_days             INTEGER NOT NULL DEFAULT 0,
    next_open_date          TEXT,
    purchase_cutoff         TEXT NOT NULL DEFAULT '15:00',
    sale_status             TEXT NOT NULL DEFAULT 'on_sale',
    updated_on              TEXT NOT NULL
);
"""
"""理财披露表（独立 DDL；不并入 01 的 `SCHEMA_SQL`）。"""

_SEEDED: tuple[tuple[object, ...], ...] = (
    # product_id, type, code, issuer, issuer_r, seller_r, sub_fee, svc_fee, custody,
    # increment, redeem_mode, fast_limit, closed_days, next_open, cutoff
    (
        "wp_cash", "cash", "DEMO-Z7000001", "演示理财公司", "R1", "R1",
        0, 20, 2, 100, "realtime", 1000000, 0, None, "15:00",
    ),
    (
        "wp_short", "bond", "DEMO-Z7000002", "演示理财公司", "R2", "R2",
        0, 30, 2, 100000, "t_plus_1", None, 30, "2026-11-30", "15:00",
    ),
    (
        "wp_bond", "bond", "DEMO-Z7000003", "演示理财公司", "R2", "R2",
        0, 30, 2, 100000, "t_plus_1", None, 90, "2026-12-30", "15:00",
    ),
    (
        "wp_mix", "mixed", "DEMO-Z7000004", "演示理财公司", "R2", "R3",
        100, 40, 2, 1000000, "t_plus_3", None, 180, "2027-04-29", "15:00",
    ),
    (
        "wp_equity", "equity", "DEMO-Z7000005", "演示理财公司", "R4", "R4",
        120, 50, 2, 1000000, "t_plus_3", None, 365, "2027-10-31", "15:00",
    ),
)
"""五只演示产品的披露事实；费率单位为基点（1 bp = 0.01%）。"""


def init_wealth_disclosure_schema(store: Store) -> None:
    """建披露表（幂等）。"""
    with store.transaction() as conn:
        conn.executescript(WEALTH_DISCLOSURE_SCHEMA_SQL)


def seed_wealth_disclosures(store: Store) -> None:
    """幂等播种披露事实（不覆盖既有行）。"""
    with store.transaction() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO wealth_disclosures("
            "product_id, product_type, registration_code, issuer, issuer_risk_level,"
            " seller_risk_level, subscription_fee_bp, service_fee_bp, custody_fee_bp,"
            " increment_cents, redeem_mode, fast_redeem_limit_cents, closed_days,"
            " next_open_date, purchase_cutoff, sale_status, updated_on"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'on_sale', ?)",
            [(*row, DEFAULT_BOOKING_DATE.isoformat()) for row in _SEEDED],
        )


def wealth_disclosures(store: Store) -> tuple[WealthDisclosure, ...]:
    """全部披露事实（按 product_id 排序）。"""
    rows = store.query("SELECT * FROM wealth_disclosures ORDER BY product_id")
    return tuple(_disclosure(row) for row in rows)


def wealth_disclosure(store: Store, product_id: str) -> WealthDisclosure | None:
    """单只产品的披露事实；缺行 ⇒ `None`（调用方按「未披露」处理）。"""
    rows = store.query(
        "SELECT * FROM wealth_disclosures WHERE product_id = ?", (product_id,)
    )
    return None if not rows else _disclosure(rows[0])


def _disclosure(row: object) -> WealthDisclosure:
    return WealthDisclosure(
        product_id=str(row["product_id"]),  # type: ignore[index]
        product_type=str(row["product_type"]),  # type: ignore[index]
        registration_code=str(row["registration_code"]),  # type: ignore[index]
        issuer=str(row["issuer"]),  # type: ignore[index]
        issuer_risk_level=str(row["issuer_risk_level"]),  # type: ignore[index]
        seller_risk_level=str(row["seller_risk_level"]),  # type: ignore[index]
        subscription_fee_bp=_optional_int(row["subscription_fee_bp"]),  # type: ignore[index]
        service_fee_bp=_optional_int(row["service_fee_bp"]),  # type: ignore[index]
        custody_fee_bp=_optional_int(row["custody_fee_bp"]),  # type: ignore[index]
        increment_cents=int(row["increment_cents"]),  # type: ignore[index]
        redeem_mode=str(row["redeem_mode"]),  # type: ignore[index]
        fast_redeem_limit_cents=_optional_int(row["fast_redeem_limit_cents"]),  # type: ignore[index]
        closed_days=int(row["closed_days"]),  # type: ignore[index]
        next_open_date=_optional_date(row["next_open_date"]),  # type: ignore[index]
        purchase_cutoff=str(row["purchase_cutoff"]),  # type: ignore[index]
        sale_status=str(row["sale_status"]),  # type: ignore[index]
        updated_on=date.fromisoformat(str(row["updated_on"])),  # type: ignore[index]
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return int(value)
    return None


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(str(value))
