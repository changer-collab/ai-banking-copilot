"""同名收款人澄清卡：候选要带「手机尾号 / 账号尾号 / 最近一次转账」，点一下就能继续。

验收点名要「138****3721 + 最近一次转账时间」，但名册只存手机后四位（`Payee.phone_tail`），
所以卡上只渲染事实：手机尾号 / 账号尾号 / 最近一次转账日期。**不补全号码中间位**——
补全就是生成事实，违反 H1。没有转账记录时如实写「暂无转账记录」。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app


def _session(deps: AppDeps) -> tuple[TestClient, str]:
    return TestClient(create_app(deps)), f"payee-{uuid.uuid4().hex[:8]}"


def _asks(payload: dict[str, object]) -> list[dict[str, object]]:
    asks = payload["clarifications"]
    assert isinstance(asks, list)
    return asks


def _payee_ask(payload: dict[str, object]) -> dict[str, object]:
    ask = next(item for item in _asks(payload) if item["slot"] == "payee")
    assert isinstance(ask["candidates"], list)
    return ask


def test_same_name_payees_come_with_clickable_candidates(deps: AppDeps) -> None:
    client, session_id = _session(deps)
    payload = client.post(
        "/api/submit", json={"session_id": session_id, "utterance": "转给王伟 500 元"}
    ).json()
    assert payload["state"] == "needs_clarification"
    ask = _payee_ask(payload)
    choices = ask["candidates"]
    assert isinstance(choices, list) and len(choices) == 2
    # 回填值只认本人名册对象 ID（自由文本会被当越权选择拒绝）；标签才是给人看的
    assert {choice["value"] for choice in choices} == {"p_wangwei_a", "p_wangwei_b"}
    labels = " ".join(str(choice["label"]) for choice in choices)
    assert "手机尾号 3301" in labels
    assert "手机尾号 7788" in labels
    assert "账号尾号 8812" in labels
    assert "账号尾号 4417" in labels
    # 名册只有后四位：不得出现被补全的完整号码
    assert "138" not in labels
    assert "暂无转账记录" in labels


def test_unknown_payee_has_no_candidates(deps: AppDeps) -> None:
    """名册里没有这个人时只追问，不给候选：候选不是「猜一个最像的」。"""
    client, session_id = _session(deps)
    payload = client.post(
        "/api/submit", json={"session_id": session_id, "utterance": "转给老王 500 元"}
    ).json()
    assert _payee_ask(payload)["candidates"] == []


def test_clicking_a_candidate_carries_the_plan(deps: AppDeps) -> None:
    client, session_id = _session(deps)
    submitted = client.post(
        "/api/submit", json={"session_id": session_id, "utterance": "转给王伟 500 元"}
    ).json()
    choices = _payee_ask(submitted)["candidates"]
    assert isinstance(choices, list)
    value = next(str(item["value"]) for item in choices if "手机尾号 3301" in str(item["label"]))
    clarified = client.post(
        "/api/clarify", json={"session_id": session_id, "answers": {"payee": value}}
    ).json()
    assert clarified["state"] == "awaiting_confirm"
    card = clarified["card"]
    assert card is not None
    assert "账号尾号 8812" in " ".join(card["lines"])


def test_recent_transfer_shows_up_after_a_real_transfer(deps: AppDeps) -> None:
    """先真转一笔给「王伟（手机尾号 3301）」，再问同一句：该候选要显示最近一次转账日期。

    只认**已记账**的转出：另一名王伟（7788）仍写「暂无转账记录」。
    """
    client, session_id = _session(deps)
    submitted = client.post(
        "/api/submit", json={"session_id": session_id, "utterance": "转给王伟 500 元"}
    ).json()
    choices = _payee_ask(submitted)["candidates"]
    assert isinstance(choices, list)
    value = next(str(item["value"]) for item in choices if "手机尾号 3301" in str(item["label"]))
    clarified = client.post(
        "/api/clarify", json={"session_id": session_id, "answers": {"payee": value}}
    ).json()
    card = clarified["card"]
    confirmed = client.post(
        "/api/confirm",
        json={
            "session_id": session_id,
            "card_id": card["card_id"],
            "frozen_hash": card["frozen_hash"],
        },
    ).json()
    assert confirmed["state"] == "completed"

    again = client.post(
        "/api/submit",
        json={"session_id": f"{session_id}-again", "utterance": "给王伟转 500 元"},
    ).json()
    choices = _payee_ask(again)["candidates"]
    assert isinstance(choices, list)
    labels = {str(item["value"]): str(item["label"]) for item in choices}
    assert "最近一次转账 2026-10-31" in labels["p_wangwei_a"]
    assert "暂无转账记录" in labels["p_wangwei_b"]
