"""客户级日限额（银行侧事实）：按客户资产分档，因人而异且永不超过官方上限。"""

from __future__ import annotations

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.policy.pdp import DAILY_LIMIT_YUAN

_OFFICIAL_CAP_CENTS = DAILY_LIMIT_YUAN * 100


def test_daily_limit_follows_asset_band(gateway: FactGateway) -> None:
    limit = gateway.daily_transfer_limit(DEMO_USER_ID)
    assert limit == Money(100_000)


def test_daily_limit_never_exceeds_official_cap(gateway: FactGateway) -> None:
    assert gateway.daily_transfer_limit(DEMO_USER_ID).cents <= _OFFICIAL_CAP_CENTS


def test_daily_limit_is_per_customer(gateway: FactGateway) -> None:
    assert gateway.daily_transfer_limit("nobody") == Money(30_000)
