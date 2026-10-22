"""P2-7 IM 渠道适配器：受信任文本进 / 出，走既有 `Session`（**能力子集**）。"""

from __future__ import annotations

import pytest

from ai_banking_copilot.gateway.store import Store
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.channels.im import ImChannel
from ai_banking_copilot.web.session import ConfirmationMismatchError


def _balance(store: Store) -> int:
    return store.query(
        "SELECT balance_cents FROM accounts WHERE account_id = 'acc_savings'"
    )[0]["balance_cents"]


def _txn_count(store: Store) -> int:
    return store.query("SELECT COUNT(*) AS n FROM transactions")[0]["n"]


def test_im_channel_handles_trusted_text(deps: AppDeps) -> None:
    channel = ImChannel(deps, session_id="im-a")
    reply = channel.handle("转给李娜 500 元")
    assert reply.session_id == "im-a"
    assert reply.state == "awaiting_confirm"
    assert "计划卡" in reply.text


def test_im_channel_reports_refusal_truthfully(deps: AppDeps) -> None:
    reply = ImChannel(deps, session_id="im-b").handle("帮我订一张去上海的机票")
    assert reply.state == "blocked"
    assert "安全拒绝" in reply.text


def test_im_channels_with_distinct_ids_do_not_interfere(deps: AppDeps) -> None:
    first = ImChannel(deps, session_id="im-1")
    second = ImChannel(deps, session_id="im-2")
    assert first.handle("转给李娜 500 元").state == "awaiting_confirm"
    assert second.handle("查一下上月账单").state == "awaiting_confirm"
    assert first.session_id != second.session_id


def test_im_submission_alone_never_writes(deps: AppDeps, rw_store: Store) -> None:
    """IM 只提交：停在待确认，账户与流水均无副作用。"""
    before_balance, before_txn = _balance(rw_store), _txn_count(rw_store)
    reply = ImChannel(deps, session_id="im-w").handle("转给李娜 500 元")
    assert reply.state == "awaiting_confirm"
    assert _balance(rw_store) == before_balance
    assert _txn_count(rw_store) == before_txn


def test_im_confirm_rejects_a_tampered_hash_without_side_effects(
    deps: AppDeps, rw_store: Store
) -> None:
    """IM confirm 与 Web 共用冻结校验：哈希不符即抛错，不写库。"""
    channel = ImChannel(deps, session_id="im-h")
    channel.handle("转给李娜 500 元")
    card = channel._session.card()  # noqa: SLF001 - 仅测试取服务端卡片绑定值
    assert card is not None
    before_balance, before_txn = _balance(rw_store), _txn_count(rw_store)
    with pytest.raises(StalePlanError):
        channel.confirm(card.card_id, frozen_hash="0" * 64)
    assert _balance(rw_store) == before_balance
    assert _txn_count(rw_store) == before_txn


def test_im_confirm_rejects_a_wrong_card_id_without_side_effects(
    deps: AppDeps, rw_store: Store
) -> None:
    channel = ImChannel(deps, session_id="im-c")
    channel.handle("转给李娜 500 元")
    card = channel._session.card()  # noqa: SLF001 - 仅测试取服务端卡片绑定值
    assert card is not None
    before_balance, before_txn = _balance(rw_store), _txn_count(rw_store)
    with pytest.raises(ConfirmationMismatchError):
        channel.confirm("card_nope", frozen_hash=card.frozen_hash)
    assert _balance(rw_store) == before_balance
    assert _txn_count(rw_store) == before_txn


def test_im_confirm_with_the_server_card_executes_once(
    deps: AppDeps, rw_store: Store
) -> None:
    """正确确认后经 `Session` → `Pep` 执行一次；重复确认不产生第二笔流水。"""
    channel = ImChannel(deps, session_id="im-ok")
    channel.handle("转给李娜 500 元")
    card = channel._session.card()  # noqa: SLF001 - 仅测试取服务端卡片绑定值
    assert card is not None
    before_balance, before_txn = _balance(rw_store), _txn_count(rw_store)

    done = channel.confirm(card.card_id, frozen_hash=card.frozen_hash)
    assert done.state == "completed"
    assert _balance(rw_store) == before_balance - 50000
    assert _txn_count(rw_store) == before_txn + 1

    again = channel.confirm(card.card_id, frozen_hash=card.frozen_hash)
    assert again.state == "completed"
    assert _balance(rw_store) == before_balance - 50000
    assert _txn_count(rw_store) == before_txn + 1
