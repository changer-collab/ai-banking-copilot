"""客户适当性属性：出生年份（65+ 特别提示的唯一事实来源）。

现库 `user_profiles.birthday` 只有 `MM-DD`，算不出年龄；本表独立存放出生年份，
不改既有生日联动表。两个演示用户分别播种 1990 / 1996，均不触发 65+ 路径；
65+ 路径由测试数据覆盖。
"""

from __future__ import annotations

from ai_banking_copilot.gateway.models import CustomerSuitabilityProfile
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, SECONDARY_USER_ID
from ai_banking_copilot.gateway.store import Store

CUSTOMER_PROFILE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS customer_suitability_profiles (
    user_id    TEXT PRIMARY KEY,
    birth_year INTEGER
);
"""

_SEEDED = ((DEMO_USER_ID, 1990), (SECONDARY_USER_ID, 1996))
"""演示用户的出生年份：陈明 1990、林静 1996；均为演示值，不接真实客户资料。"""


def init_customer_profile_schema(store: Store) -> None:
    with store.transaction() as conn:
        conn.executescript(CUSTOMER_PROFILE_SCHEMA_SQL)


def seed_customer_profiles(store: Store) -> None:
    """幂等播种；不覆盖既有行。"""
    with store.transaction() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO customer_suitability_profiles(user_id, birth_year)"
            " VALUES(?, ?)",
            _SEEDED,
        )


def customer_suitability_profile(
    store: Store, user_id: str
) -> CustomerSuitabilityProfile | None:
    """按用户读出生年份；缺行 ⇒ `None`（不猜年龄）。"""
    rows = store.query(
        "SELECT user_id, birth_year FROM customer_suitability_profiles WHERE user_id = ?",
        (user_id,),
    )
    if not rows:
        return None
    raw = rows[0]["birth_year"]
    birth_year = int(raw) if raw is not None else None
    return CustomerSuitabilityProfile(user_id=str(rows[0]["user_id"]), birth_year=birth_year)
