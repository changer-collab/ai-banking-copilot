"""订阅识别详情：改名 / 调价 / 日期偏移 / 数据不足 / 疑似不升级的验收。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import cast

import pytest

from ai_banking_copilot.contracts.taint import Trust, Value
from ai_banking_copilot.gateway.facts import FactGateway, Record
from ai_banking_copilot.gateway.models import Account, Money, Subscription, Transaction
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import ScenarioRefused
from ai_banking_copilot.scenarios.subscriptions import (
    CANCEL_STATE_ALREADY_LOCAL,
    CANCEL_STATE_EXTERNAL_CONFIRMED,
    CANCEL_STATE_GUIDANCE,
    CANCEL_STATE_LABELS,
    CANCEL_STATE_LOCAL_CANCELLED,
    CANCEL_STATE_REQUESTED,
    CANCEL_STATE_THIRD_PARTY_PAGE,
    EXTERNAL_CANCEL_CONFIRMED,
    EXTERNAL_CANCEL_NOT_INTEGRATED,
    INSIGHT_CONFIRMED,
    INSIGHT_SUSPECTED,
    SubscriptionInsight,
    SubscriptionsHandler,
    cancel_state_from_receipt,
    insights_payload,
    subscription_report,
)

USER_ID = "u_stub"
ACCOUNT_ID = "acc_stub"
AS_OF = date(2026, 10, 21)


class _StubGateway:
    """只含一个账户、给定流水与订阅登记的假事实源（不触库、不联网）。"""

    def __init__(
        self,
        *,
        transactions: tuple[Transaction, ...] = (),
        subscriptions: tuple[Subscription, ...] = (),
    ) -> None:
        self._account = Record(
            record_id=f"accounts:{ACCOUNT_ID}",
            payload=Account(ACCOUNT_ID, USER_ID, "savings", Money(0)),
            content_hash="h-account",
        )
        self._transactions = tuple(
            Record(
                record_id=f"transactions:{txn.txn_id}", payload=txn, content_hash=f"h-{txn.txn_id}"
            )
            for txn in transactions
        )
        self._subscriptions = tuple(
            Record(
                record_id=f"subscriptions:{item.subscription_id}",
                payload=item,
                content_hash=f"h-{item.subscription_id}",
            )
            for item in subscriptions
        )

    def accounts_of(self, user_id: str) -> tuple[Record[Account], ...]:
        return (self._account,) if user_id == USER_ID else ()

    def transactions_between(
        self, account_id: str, start: date, end: date
    ) -> tuple[Record[Transaction], ...]:
        return tuple(
            record
            for record in self._transactions
            if record.payload.account_id == account_id
            and start <= record.payload.booked_on <= end
        )

    def subscriptions_of(self, user_id: str) -> tuple[Record[Subscription], ...]:
        return tuple(
            record for record in self._subscriptions if record.payload.user_id == user_id
        )


def _gateway(
    *,
    transactions: tuple[Transaction, ...] = (),
    subscriptions: tuple[Subscription, ...] = (),
) -> FactGateway:
    return cast(
        FactGateway,
        _StubGateway(transactions=transactions, subscriptions=subscriptions),
    )


def _txn(
    txn_id: str, booked_on: date, cents: int, merchant: str, *, category: str = "会员服务"
) -> Transaction:
    return Transaction(
        txn_id=txn_id,
        user_id=USER_ID,
        account_id=ACCOUNT_ID,
        booked_on=booked_on,
        amount=Money(-cents),
        merchant=merchant,
        category=category,
        channel="card",
        direction="out",
    )


def _subscription(
    merchant: str,
    cents: int,
    next_charge_on: date,
    *,
    subscription_id: str = "sub_stub",
) -> Subscription:
    return Subscription(
        subscription_id=subscription_id,
        user_id=USER_ID,
        merchant=merchant,
        amount=Money(cents),
        period="monthly",
        next_charge_on=next_charge_on,
        status="active",
    )


def _only_insight(gateway: FactGateway) -> SubscriptionInsight:
    report = subscription_report(gateway, USER_ID, as_of=AS_OF)
    assert len(report.insights) == 1, report
    return report.insights[0]


def _codes(insight: SubscriptionInsight) -> set[str]:
    return {notice.code for notice in insight.notices}


@pytest.mark.parametrize(
    ("first_cents", "last_cents", "increased"),
    [(2500, 3000, True), (3000, 2500, False), (2500, 2500, False), (2500, 2501, True)],
)
def test_price_change_12m_compares_first_and_last_charge(
    first_cents: int, last_cents: int, increased: bool,
) -> None:
    insight = _only_insight(_gateway(
        subscriptions=(_subscription("星光视频", 2500, AS_OF),),
        transactions=(
            _txn("first", date(2026, 7, 18), first_cents, "星光视频"),
            _txn("middle", date(2026, 8, 18), 5000, "星光视频"),
            _txn("last", date(2026, 9, 18), last_cents, "星光视频"),
        ),
    ))
    assert insight.price_increased_12m is increased
    assert insight.price_change_12m_from_cents == first_cents
    assert insight.price_change_12m_to_cents == last_cents


@pytest.mark.parametrize("count", [0, 1])
def test_price_change_12m_is_unknown_with_fewer_than_two_charges(count: int) -> None:
    insight = _only_insight(_gateway(
        subscriptions=(_subscription("星光视频", 2500, AS_OF),),
        transactions=(_txn("only", date(2026, 9, 18), 2500, "星光视频"),)[:count],
    ))
    assert insight.price_increased_12m is None
    assert insight.price_change_12m_from_cents is None
    assert insight.price_change_12m_to_cents is None


@pytest.mark.parametrize(
    ("as_of", "start"),
    [
        (AS_OF, date(2025, 10, 21)),
        (date(2024, 2, 29), date(2023, 2, 28)),
        (date(2025, 2, 28), date(2024, 2, 28)),
    ],
)
def test_price_change_12m_uses_closed_calendar_month_window(
    as_of: date, start: date,
) -> None:
    report = subscription_report(_gateway(
        subscriptions=(_subscription("星光视频", 2500, as_of),),
        transactions=(
            _txn("before", start - timedelta(days=1), 1000, "星光视频"),
            _txn("start", start, 2500, "星光视频"),
            _txn("end", as_of, 3000, "星光视频"),
            _txn("future", as_of + timedelta(days=1), 9000, "星光视频"),
        ),
    ), USER_ID, as_of=as_of)
    insight = report.insights[0]
    assert insight.price_increased_12m is True
    assert insight.price_change_12m_from_cents == 2500
    assert insight.price_change_12m_to_cents == 3000
    # 窗口只用于新字段，既有历史证据、计数和中位数调价提示仍保留。
    assert insight.charge_count == 3
    assert [item.txn_id for item in insight.evidence] == ["before", "start", "end"]
    notice = next(item for item in insight.notices if item.code == "amount-change")
    assert "17.50 元变为 30.00 元" in notice.message


def test_price_change_12m_ignores_other_users_merchants_and_inflows() -> None:
    first = _txn("first", date(2026, 7, 18), 2500, "星光视频")
    insight = _only_insight(_gateway(
        subscriptions=(_subscription("星光视频", 2500, AS_OF),),
        transactions=(
            first,
            _txn("last", date(2026, 9, 18), 3000, "星光视频"),
            replace(first, txn_id="other-user", user_id="u_other", amount=Money(-100)),
            replace(first, txn_id="other-merchant", merchant="咖啡店", amount=Money(-100)),
            replace(first, txn_id="inflow", direction="in", amount=Money(100)),
        ),
    ))
    assert insight.price_increased_12m is True
    assert insight.price_change_12m_from_cents == 2500
    assert insight.price_change_12m_to_cents == 3000
    assert [item.txn_id for item in insight.evidence] == ["first", "last"]


def test_same_day_charges_sort_by_time_then_transaction_id_and_keep_evidence() -> None:
    day = date(2026, 9, 18)
    early = replace(
        _txn("z-early", day, 2500, "星光视频"),
        booked_at="2026-09-18T09:00:00", card_tail="4821",
    )
    late = replace(early, txn_id="b-late", amount=Money(-3000), booked_at="2026-09-18T20:30:00")
    legacy = _txn("zz-legacy", day, 2400, "星光视频")
    insight = _only_insight(_gateway(
        subscriptions=(_subscription("星光视频", 2500, AS_OF),),
        transactions=(late, replace(late, txn_id="a-late", amount=Money(-2700)), early, legacy),
    ))
    assert [item.txn_id for item in insight.evidence] == [
        "zz-legacy", "z-early", "a-late", "b-late",
    ]
    assert insight.price_increased_12m is True
    assert insight.price_change_12m_from_cents == 2400
    assert insight.price_change_12m_to_cents == 3000
    assert insight.evidence[0].booked_at is None
    assert insight.evidence[0].card_tail is None
    assert insight.evidence[-1].booked_at == late.booked_at
    assert insight.evidence[-1].card_tail == "4821"


def test_unregistered_insight_includes_price_change_12m() -> None:
    insight = _only_insight(_gateway(transactions=(
        _txn("first", date(2026, 8, 18), 2500, "悦读会员"),
        _txn("last", date(2026, 9, 18), 2600, "悦读会员"),
    )))
    assert insight.status == INSIGHT_SUSPECTED
    assert insight.price_increased_12m is True
    assert insight.price_change_12m_from_cents == 2500
    assert insight.price_change_12m_to_cents == 2600
    assert "amount-change" not in _codes(insight)


# 1. 商户改名：仍能识别，但降级为疑似并给出改名提示
def test_renamed_merchant_is_detected_but_downgraded_to_suspected() -> None:
    gateway = _gateway(
        subscriptions=(_subscription("星光视频", 2500, date(2026, 10, 18)),),
        transactions=(
            _txn("t1", date(2026, 6, 18), 2500, "星芒视频"),
            _txn("t2", date(2026, 7, 18), 2500, "星芒视频"),
            _txn("t3", date(2026, 8, 18), 2500, "星芒视频"),
            _txn("t4", date(2026, 9, 18), 2500, "星芒视频"),
        ),
    )
    insight = _only_insight(gateway)
    assert insight.subscription_id == "sub_stub"
    assert insight.status == INSIGHT_SUSPECTED
    assert insight.confidence == Decimal("0.70")
    assert insight.last_charged_on == date(2026, 9, 18)
    assert "merchant-renamed" in _codes(insight)
    assert [item.txn_id for item in insight.evidence][-1] == "t4"


# 2. 金额上涨：给出调价提示，并与登记金额不一致分别提示
def test_amount_increase_is_flagged_against_registered_amount() -> None:
    gateway = _gateway(
        subscriptions=(_subscription("云听音乐", 1000, date(2026, 10, 15)),),
        transactions=(
            _txn("t1", date(2026, 7, 15), 1000, "云听音乐"),
            _txn("t2", date(2026, 8, 15), 1000, "云听音乐"),
            _txn("t3", date(2026, 9, 15), 1200, "云听音乐"),
        ),
    )
    insight = _only_insight(gateway)
    assert insight.status == INSIGHT_CONFIRMED
    assert insight.amount_cents == 1200
    assert {"amount-change", "registered-amount-mismatch"} <= _codes(insight)
    assert "调价" in insight.suggestion


# 3. 日期偏移：给出偏差提示，下次扣费必须是带依据的预测值
def test_date_offset_is_flagged_and_next_charge_is_prediction() -> None:
    gateway = _gateway(
        subscriptions=(_subscription("星云网盘", 1980, date(2026, 10, 1)),),
        transactions=(
            _txn("t1", date(2026, 7, 8), 1980, "星云网盘"),
            _txn("t2", date(2026, 8, 8), 1980, "星云网盘"),
            _txn("t3", date(2026, 9, 8), 1980, "星云网盘"),
        ),
    )
    insight = _only_insight(gateway)
    assert "date-offset" in _codes(insight)
    assert insight.next_charge_is_prediction is True
    assert insight.next_charge_on == date(2026, 10, 9)
    assert "预测" in insight.next_charge_basis


# 4. 扣费周期改变：最近间隔明显偏离历史中位间隔
def test_cycle_change_is_flagged() -> None:
    gateway = _gateway(
        subscriptions=(_subscription("悦读会员", 1500, date(2026, 10, 1)),),
        transactions=(
            _txn("t1", date(2026, 5, 1), 1500, "悦读会员"),
            _txn("t2", date(2026, 6, 1), 1500, "悦读会员"),
            _txn("t3", date(2026, 7, 1), 1500, "悦读会员"),
            _txn("t4", date(2026, 9, 15), 1500, "悦读会员"),
        ),
    )
    assert "cycle-change" in _codes(_only_insight(gateway))


# 5. 数据不足：不得升级为已确认，只给提示
def test_single_charge_keeps_registered_status_with_explicit_notice() -> None:
    gateway = _gateway(
        subscriptions=(_subscription("星光视频", 2500, date(2026, 10, 18)),),
        transactions=(_txn("t1", date(2026, 9, 18), 2500, "星光视频"),),
    )
    insight = _only_insight(gateway)
    assert insight.status == INSIGHT_CONFIRMED
    assert insight.confidence == Decimal("0.85")
    assert "insufficient-data" in _codes(insight)


def test_unregistered_single_charge_is_not_listed_as_subscription() -> None:
    gateway = _gateway(transactions=(_txn("t1", date(2026, 9, 18), 1500, "悦读会员"),))
    report = subscription_report(gateway, USER_ID, as_of=AS_OF)
    assert report.insights == ()
    assert any("不足以判断扣费周期" in note for note in report.notes)


# 6. 未登记但周期性扣费：只能标为疑似，绝不自动升级
def test_unregistered_recurring_charge_is_only_suspected() -> None:
    gateway = _gateway(
        transactions=(
            _txn("t1", date(2026, 7, 5), 1500, "悦读会员"),
            _txn("t2", date(2026, 8, 5), 1500, "悦读会员"),
            _txn("t3", date(2026, 9, 5), 1500, "悦读会员"),
        )
    )
    insight = _only_insight(gateway)
    assert insight.status == INSIGHT_SUSPECTED
    assert insight.subscription_id is None
    assert insight.confidence == Decimal("0.60")
    assert insight.next_charge_is_prediction is True
    assert insight.cancel_action == "merchant_self_service"
    assert insight.external_cancel_status == EXTERNAL_CANCEL_NOT_INTEGRATED
    assert "unregistered" in _codes(insight)


# 7. 大额周期消费不误报为订阅
def test_large_recurring_spend_is_not_reported_as_subscription() -> None:
    gateway = _gateway(
        transactions=(
            _txn("t1", date(2026, 7, 5), 50_000, "家电商城", category="购物"),
            _txn("t2", date(2026, 8, 5), 50_000, "家电商城", category="购物"),
            _txn("t3", date(2026, 9, 5), 50_000, "家电商城", category="购物"),
        )
    )
    assert subscription_report(gateway, USER_ID, as_of=AS_OF).insights == ()


# 8. seed：预测值标注、无流水退回登记值、证据与原始流水一致
def test_seed_report_marks_prediction_and_matches_raw_transactions(
    gateway: FactGateway,
) -> None:
    report = subscription_report(gateway, DEMO_USER_ID, as_of=date(2026, 10, 1))
    by_id = {item.subscription_id: item for item in report.insights}

    video = by_id["sub_video"]
    assert video.status == INSIGHT_CONFIRMED
    assert video.next_charge_is_prediction is True
    assert "预测" in video.next_charge_basis
    assert {"amount-change", "date-offset"} <= _codes(video)

    music = by_id["sub_music"]
    assert music.charge_count == 0
    assert music.next_charge_is_prediction is False
    assert music.next_charge_on == date(2026, 11, 15)
    assert "insufficient-data" in _codes(music)

    for evidence in video.evidence:
        record = gateway.transaction(evidence.txn_id)
        assert evidence.record_id == record.record_id
        assert evidence.content_hash == record.content_hash
        assert evidence.amount_cents == record.payload.amount.cents
        assert evidence.booked_on == record.payload.booked_on
        assert evidence.channel == record.payload.channel


def test_payload_marks_demo_data_and_unintegrated_external_cancel(
    gateway: FactGateway,
) -> None:
    payload = insights_payload(subscription_report(gateway, DEMO_USER_ID, as_of=date(2026, 10, 1)))
    assert payload["data_source_is_demo"] is True
    assert payload["external_cancel_status"] == EXTERNAL_CANCEL_NOT_INTEGRATED
    insights = cast(list[dict[str, object]], payload["insights"])
    video = next(
        item for item in insights if item["insight_id"] == "subscription:sub_video"
    )
    assert video["amount_yuan"] == "25.00"
    assert video["currency"] == "CNY"
    assert video["evidence"]
    assert cast(list[dict[str, object]], video["evidence"])[0]["channel"] == "app"


# 9. 取消状态互不混用；失败 / 超时不得表述为已取消
def test_cancel_states_are_distinct_and_failures_are_not_cancelled() -> None:
    assert cancel_state_from_receipt(
        status="cancelled", external_status=EXTERNAL_CANCEL_NOT_INTEGRATED
    ) == CANCEL_STATE_LOCAL_CANCELLED
    assert cancel_state_from_receipt(
        status="already_cancelled", external_status=EXTERNAL_CANCEL_NOT_INTEGRATED
    ) == CANCEL_STATE_ALREADY_LOCAL
    assert cancel_state_from_receipt(
        status="cancelled", external_status=EXTERNAL_CANCEL_CONFIRMED
    ) == CANCEL_STATE_EXTERNAL_CONFIRMED
    for failure in ("failed", "timeout", "unavailable", "unknown"):
        assert cancel_state_from_receipt(
            status=failure, external_status=EXTERNAL_CANCEL_NOT_INTEGRATED
        ) == CANCEL_STATE_REQUESTED

    states = [
        CANCEL_STATE_GUIDANCE,
        CANCEL_STATE_THIRD_PARTY_PAGE,
        CANCEL_STATE_REQUESTED,
        CANCEL_STATE_LOCAL_CANCELLED,
        CANCEL_STATE_ALREADY_LOCAL,
        CANCEL_STATE_EXTERNAL_CONFIRMED,
    ]
    assert len(set(states)) == len(states)
    assert set(CANCEL_STATE_LABELS) == set(states)
    assert len(set(CANCEL_STATE_LABELS.values())) == len(states)


# 10. 跨用户取消必须失败（归属校验，G-01）
def test_cancel_of_another_users_subscription_is_refused(gateway: FactGateway) -> None:
    intent = Intent(
        action="subscription.cancel",
        slots={
            "span": "取消订阅",
            "subscription": Value("sub_video", Trust.TRUSTED),
            "user_id": Value("u_other", Trust.TRUSTED),
        },
        confidence=0.9,
        evidence="取消订阅",
    )
    with pytest.raises(ScenarioRefused):
        SubscriptionsHandler().build([intent], gateway)
