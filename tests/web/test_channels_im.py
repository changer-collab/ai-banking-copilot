"""P2-7 IM 渠道适配器：受信任文本进 / 出，走既有 `Session`（**能力子集**）。"""

from __future__ import annotations

import re
from contextlib import ExitStack
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from ai_banking_copilot.compose import build_deps
from ai_banking_copilot.gateway.store import Store, StoreMode
from ai_banking_copilot.policy.gate import StalePlanError
from ai_banking_copilot.web.app import AppDeps, create_app
from ai_banking_copilot.web.channels.im import ImChannel
from ai_banking_copilot.web.session import ConfirmationMismatchError

_TEMPLATE_ENV = {"AIB_DISABLE_LLM": "1", "AIB_BREAKER_STATE": "memory"}


def _card_contract(body: dict[str, object]) -> dict[str, object]:
    card = cast(dict[str, object], body["card"])
    return {
        "state": body["state"],
        "level": card["level"],
        "lines": card["lines"],
        "summary": card["summary"],
        "requires": card["requires"],
        "balance_trace": card["balance_trace"],
    }


def _balance(store: Store) -> int:
    return store.query(
        "SELECT balance_cents FROM accounts WHERE account_id = 'acc_savings'"
    )[0]["balance_cents"]


def _txn_count(store: Store) -> int:
    return store.query("SELECT COUNT(*) AS n FROM transactions")[0]["n"]


def _stored_facts(db_path: Path) -> tuple[int, int]:
    store = Store(db_path, StoreMode.READ_ONLY)
    try:
        return _balance(store), _txn_count(store)
    finally:
        store.close()


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


def test_web_and_im_return_the_same_complete_plan_card_contract(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    utterance = "转给李娜 500 元"

    web_response = client.post(
        "/api/submit",
        json={"session_id": "web-contract", "utterance": utterance},
    )
    im_response = client.post(
        "/api/im/message",
        json={"chat_id": "im-contract", "text": utterance},
    )

    assert web_response.status_code == im_response.status_code == 200
    web_body = cast(dict[str, object], web_response.json())
    im_body = cast(dict[str, object], im_response.json())
    assert _card_contract(web_body) == _card_contract(im_body)

    web_card = cast(dict[str, object], web_body["card"])
    im_card = cast(dict[str, object], im_body["card"])
    assert set(web_card) == set(im_card)
    assert web_card["frozen_hash"] == im_card["frozen_hash"]
    for card in (web_card, im_card):
        assert re.fullmatch(
            r"card_[0-9a-f]{12}_(green|yellow|red)_[0-9a-f]{32}",
            cast(str, card["card_id"]),
        )
        assert isinstance(card["frozen_hash"], str)
        assert len(card["frozen_hash"]) == 64


def test_im_uses_template_model_when_llm_is_explicitly_disabled(tmp_path: Path) -> None:
    with ExitStack() as resources:
        deps = build_deps(
            tmp_path / "im-template.db", env=_TEMPLATE_ENV, resources=resources
        )
        client = TestClient(create_app(deps))

        response = client.post(
            "/api/im/message",
            json={"chat_id": "template", "text": "转给李娜 500 元"},
        )

        assert response.status_code == 200
        body = cast(dict[str, object], response.json())
        assert body["state"] == "awaiting_confirm"
        card = cast(dict[str, object], body["card"])
        summary = cast(list[dict[str, object]], card["summary"])
        assert summary[0] == {"label": "动作", "value": "转账", "emphasis": False}
        assert card["level"] == "YELLOW"
        assert cast(str, card["card_id"])
        assert cast(str, card["frozen_hash"])


def test_im_confirmation_survives_restart_and_executes_once(tmp_path: Path) -> None:
    db_path = tmp_path / "im-restart.db"
    chat_id = "restart"
    before_balance: int
    before_txn: int

    with ExitStack() as resources:
        deps = build_deps(db_path, env=_TEMPLATE_ENV, resources=resources)
        with TestClient(create_app(deps)) as client:
            submitted = client.post(
                "/api/im/message",
                json={"chat_id": chat_id, "text": "转给李娜 500 元"},
            )
            assert submitted.status_code == 200
            card = cast(dict[str, object], submitted.json()["card"])
            before_balance, before_txn = _stored_facts(db_path)

    with ExitStack() as resources:
        deps = build_deps(db_path, env=_TEMPLATE_ENV, resources=resources)
        with TestClient(create_app(deps)) as client:
            confirmation = {
                "chat_id": chat_id,
                "card_id": card["card_id"],
                "frozen_hash": card["frozen_hash"],
            }
            confirmed = client.post("/api/im/confirm", json=confirmation)
            replayed = client.post("/api/im/confirm", json=confirmation)

            assert confirmed.status_code == replayed.status_code == 200
            body = cast(dict[str, object], confirmed.json())
            replay_body = cast(dict[str, object], replayed.json())
            assert body["state"] == replay_body["state"] == "completed"
            receipt_card = cast(dict[str, object], body["card"])
            assert receipt_card["card_id"] == card["card_id"]
            balance, txn_count = _stored_facts(db_path)
            assert balance == before_balance - 50000
            assert txn_count == before_txn + 1
