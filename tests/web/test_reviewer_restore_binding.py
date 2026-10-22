"""待确认快照恢复必须绑定当前 Reviewer 通道。"""

from dataclasses import replace
from threading import RLock

import pytest

from ai_banking_copilot.planning.reviewer import (
    ReviewChannel,
    ReviewerFailure,
    ReviewReport,
    ReviewRequest,
    ReviewStatus,
)
from ai_banking_copilot.web.session import Session, SessionState, SessionStateError


class _ChannelReviewer:
    def __init__(self, channel: ReviewChannel) -> None:
        self.channel = channel

    def review(self, request: ReviewRequest) -> ReviewReport:
        return ReviewReport(
            execution_id=request.execution_id,
            cfhash=request.cfhash,
            prompt_version=request.prompt_version,
            status=ReviewStatus.ALLOW,
            channel=self.channel.value,
        )


@pytest.mark.parametrize(
    ("saved_channel", "current_channel", "reviewer_required"),
    [
        (ReviewChannel.LLM_STRICT, ReviewChannel.TEMPLATE, True),
        (ReviewChannel.TEMPLATE, ReviewChannel.LLM_STRICT, True),
        (ReviewChannel.LLM_STRICT, ReviewChannel.TEMPLATE, False),
    ],
)
def test_restore_takes_over_when_reviewer_channel_changed(
    deps, rw_store, saved_channel: ReviewChannel, current_channel: ReviewChannel,
    reviewer_required: bool,
) -> None:
    original = Session(replace(
        deps, reviewer=_ChannelReviewer(saved_channel), reviewer_required=True,
    ))
    submitted = original.submit("转给李娜 200 元", selected={"account": "acc_savings"})
    assert submitted.state is SessionState.AWAITING_CONFIRM
    assert submitted.card is not None
    snapshot = original.snapshot()

    restored = Session.restore(
        replace(
            deps,
            reviewer=_ChannelReviewer(current_channel),
            reviewer_required=reviewer_required,
        ),
        snapshot,
        RLock(),
    )

    assert restored.state() is SessionState.TAKEN_OVER
    report = restored.review_report()
    assert report is not None
    assert report.status is ReviewStatus.MANUAL
    assert report.failure_code is ReviewerFailure.BINDING_MISMATCH
    assert report.channel == saved_channel.value
    with pytest.raises(SessionStateError):
        restored.confirm(submitted.card.card_id, frozen_hash=submitted.card.frozen_hash)
    assert not rw_store.query("SELECT * FROM transactions WHERE channel='agent'")


def test_restore_keeps_pending_plan_when_reviewer_channel_matches(deps) -> None:
    reviewer = _ChannelReviewer(ReviewChannel.LLM_STRICT)
    original = Session(replace(deps, reviewer=reviewer, reviewer_required=True))
    submitted = original.submit("转给李娜 200 元", selected={"account": "acc_savings"})
    assert submitted.state is SessionState.AWAITING_CONFIRM
    snapshot = original.snapshot()

    restored = Session.restore(
        replace(deps, reviewer=_ChannelReviewer(ReviewChannel.LLM_STRICT), reviewer_required=True),
        snapshot,
        RLock(),
    )

    assert restored.state() is SessionState.AWAITING_CONFIRM
    report = restored.review_report()
    assert report is not None
    assert report.status is ReviewStatus.ALLOW


def test_restore_takes_over_when_current_reviewer_channel_is_unknown(deps) -> None:
    original = Session(replace(
        deps,
        reviewer=_ChannelReviewer(ReviewChannel.LLM_STRICT),
        reviewer_required=True,
    ))
    submitted = original.submit("转给李娜 200 元", selected={"account": "acc_savings"})
    assert submitted.card is not None

    restored = Session.restore(
        replace(deps, reviewer=None, reviewer_required=False),
        original.snapshot(),
        RLock(),
    )

    assert restored.state() is SessionState.TAKEN_OVER
    report = restored.review_report()
    assert report is not None
    assert report.status is ReviewStatus.MANUAL
