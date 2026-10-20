"""严格审查的会话执行边界，全程不联网。"""

from dataclasses import replace

import pytest

from ai_banking_copilot.planning.reviewer import ReviewReport, ReviewRequest, ReviewStatus
from ai_banking_copilot.web.session import Session, SessionState


def test_required_missing_reviewer_never_authorizes_write(deps, rw_store):
    session = Session(replace(deps, reviewer=None, reviewer_required=True))
    view = session.submit("转给李娜 500 元")
    assert view.state is SessionState.TAKEN_OVER
    assert not rw_store.query("SELECT * FROM transactions WHERE channel='agent'")


@pytest.mark.parametrize("invalid", ["diagnostic", "execution", "hash", "prompt"])
def test_required_invalid_allow_never_authorizes_write(deps, rw_store, invalid):
    class Reviewer:
        def review(self, request: ReviewRequest) -> ReviewReport:
            report = ReviewReport(
                execution_id=request.execution_id,
                cfhash=request.cfhash,
                prompt_version=request.prompt_version,
                status=ReviewStatus.ALLOW,
                channel="llm-strict",
            )
            changes = {
                "diagnostic": {"diagnostic_only": True},
                "execution": {"execution_id": "another"},
                "hash": {"cfhash": "0" * 64},
                "prompt": {"prompt_version": "unknown"},
            }
            return replace(report, **changes[invalid])

    session = Session(replace(deps, reviewer=Reviewer(), reviewer_required=True))
    view = session.submit("转给李娜 500 元")
    assert view.state is SessionState.TAKEN_OVER
    assert not rw_store.query("SELECT * FROM transactions WHERE channel='agent'")


def test_reviewer_exception_never_leaks_secret(deps, audit):
    class Reviewer:
        def review(self, request: ReviewRequest) -> ReviewReport:
            raise RuntimeError("private-provider-token")

    session = Session(replace(deps, reviewer=Reviewer(), reviewer_required=True))
    view = session.submit("转给李娜 500 元")
    assert view.state is SessionState.TAKEN_OVER
    assert "private-provider-token" not in view.message
    assert "private-provider-token" not in audit.path.read_text(encoding="utf-8")
