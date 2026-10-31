"""商户下单与用户生日资料：独立持久化模块（**不改** 01 的 `SCHEMA_SQL`）。

口径见 `docs/archive/plans/11-p2-scenario-coverage.md` §2.4（「各自独立的表 / 文件」）。

- `merchant_orders`：`merchant.order` 写动作的落点（PK `order_id`）；由
  `CoreBank.place_merchant_order` 写入，运行期经 `FactGateway` 回读做**出处校验**（H1）；
- `user_profiles`：生日联动（P2-5）的数据源（PK `user_id`，`birthday` 形如 `MM-DD`）；
  由本模块**幂等播种一个演示值**，运行期只读。

两表均自带 DDL + 幂等 `init`，**不并入** 01 的 `SCHEMA_SQL`（照 `gateway/risk_store.py` 先例）。

**诚实性声明**：`user_profiles` 当前只有**演示播种值**，不接真实客户资料（生日来源为演示数据）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store

MERCHANT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS merchant_orders (
    order_id     TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(user_id),
    merchant     TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    order_on     TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('placed', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id  TEXT PRIMARY KEY REFERENCES users(user_id),
    birthday TEXT NOT NULL
);
"""
"""商户下单 / 用户生日资料两表（独立 DDL；不并入 01 的 `SCHEMA_SQL`）。"""

MERCHANT_TABLE = "merchant_orders"
PROFILE_TABLE = "user_profiles"

DEMO_BIRTHDAY = "11-02"
"""演示生日（`MM-DD`）：使「生日前 2 天」恰好落在统一基准日（2026-10-31）。"""


@dataclass(frozen=True)
class MerchantOrder:
    """一条商户下单事实（`merchant_orders` 行）。"""

    order_id: str
    user_id: str
    merchant: str
    amount_cents: int
    order_on: str
    status: str


@dataclass(frozen=True)
class UserProfile:
    """用户资料（`user_profiles` 行）；`birthday` 形如 `MM-DD`。"""

    user_id: str
    birthday: str


def init_merchant_schema(store: Store) -> None:
    """建商户下单 / 用户资料两表（幂等；既有库自动补表）。"""
    with store.transaction() as conn:
        conn.executescript(MERCHANT_SCHEMA_SQL)


def seed_user_profiles(store: Store) -> None:
    """幂等播种演示资料行（既有库亦补行）。"""
    with store.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_profiles(user_id, birthday) VALUES(?, ?)",
            (DEMO_USER_ID, DEMO_BIRTHDAY),
        )
