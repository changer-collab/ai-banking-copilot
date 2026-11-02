"""账单异常识别接口：只读、金额可重算、模型只解释且失败必须显式降级。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from ai_banking_copilot.scenarios.bill_findings import (
    NARRATIVE_SUMMARY_ID,
    BillFindingsReport,
    narrative_choice_ids,
)
from ai_banking_copilot.web.app import AppDeps, create_app

_TABLES = ("accounts", "transactions", "subscriptions", "write_receipts")


def _snapshot(db_path: Path) -> dict[str, list[tuple[object, ...]]]:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in _TABLES
        }


def _submit(client: TestClient, utterance: str) -> dict[str, object]:
    response = client.post("/api/submit", json={"utterance": utterance})
    assert response.status_code == 200
    return response.json()


def _confirm(client: TestClient, submitted: dict[str, object], **extra: object) -> dict:
    card = submitted["card"]
    assert isinstance(card, dict)
    response = client.post("/api/confirm", json={
        "card_id": card["card_id"], "frozen_hash": card["frozen_hash"], **extra,
    })
    assert response.status_code == 200
    return response.json()


class _Narrator:
    """按 `finding_id` 生成模型输出；`override` 用于构造违规响应。"""

    def __init__(self, *, override: Mapping[str, object] | None = None) -> None:
        self._override = override
        self.calls = 0

    def narrate(self, report: BillFindingsReport) -> Mapping[str, object]:
        self.calls += 1
        if self._override is not None:
            return self._override
        return {
            "summary_id": NARRATIVE_SUMMARY_ID,
            "findings": [
                {
                    "finding_id": finding.finding_id,
                    "explanation_id": narrative_choice_ids(finding)["explanation"][0],
                    "suggestion_id": narrative_choice_ids(finding)["suggestion"][0],
                }
                for finding in report.findings
            ],
        }


def test_analysis_is_read_only_and_defaults_to_current_month(
    deps: AppDeps, db_path: Path
) -> None:
    before = _snapshot(db_path)
    client = TestClient(create_app(deps))
    response = client.get("/api/bills/analysis")
    assert response.status_code == 200
    body = response.json()

    assert body["period"] == "2026-10"
    assert body["period_basis"] == "current_month"
    assert body["data_through"] == "2026-10-31"
    assert body["data_source_is_demo"] is True
    assert "演示" in body["data_source"]
    assert body["as_of"] == "2026-10-31"
    assert body["transactions"]
    assert body["confirmed_saving_cents"] == 0
    summary = body["summary"]
    assert summary["transaction_count"] == len(body["transactions"])
    assert summary["expense_total_cents"] == sum(
        -item["amount_cents"] for item in body["transactions"]
    )
    assert summary["confirmed_count"] == sum(
        1 for item in body["findings"] if item["status"] == "confirmed"
    )
    assert summary["suspected_count"] == sum(
        1 for item in body["findings"] if item["status"] == "suspected"
    )
    assert summary["normal_count"] == sum(
        1 for item in body["findings"] if item["status"] == "normal"
    )
    assert summary["lookback_months"] == 12
    assert _snapshot(db_path) == before
    assert deps.audit.verify_chain() is True


def test_invalid_period_is_rejected(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    assert client.get("/api/bills/analysis", params={"month": 13}).status_code == 400
    assert client.get("/api/bills/analysis", params={"year": 1999}).status_code == 400
    assert client.get("/api/bills/analysis", params={"month": 0}).status_code == 400


def test_missing_narrator_degrades_explicitly(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    narrative = client.get("/api/bills/analysis").json()["narrative"]
    assert narrative["degraded"] is True
    assert narrative["channel"] == "deterministic"
    assert "未装配" in narrative["degrade_reason"]


def test_valid_narrative_explains_without_changing_amounts(deps: AppDeps) -> None:
    baseline = TestClient(create_app(deps)).get(
        "/api/bills/analysis", params={"year": 2026, "month": 1},
    ).json()
    narrator = _Narrator()
    client = TestClient(create_app(replace(deps, bill_narrator=narrator)))
    body = client.get("/api/bills/analysis", params={"year": 2026, "month": 1}).json()

    assert narrator.calls == 1
    assert body["narrative"]["degraded"] is False
    assert body["narrative"]["channel"] == "llm"
    assert body["confirmed_saving_cents"] == baseline["confirmed_saving_cents"]
    assert body["potential_saving_cents"] == baseline["potential_saving_cents"]
    assert body["potential_saving_cents"] > 0
    assert body["findings"] == baseline["findings"]
    assert {item["finding_id"] for item in body["narrative"]["findings"]} == {
        item["finding_id"] for item in body["findings"]
    }


def test_narrative_with_invented_evidence_is_rejected(deps: AppDeps) -> None:
    forged = {
        "summary": "本期账单存在需要核对的扣费项。",
        "findings": [
            {
                "finding_id": "duplicate-charge-99",
                "explanation": "同一商户重复扣费。",
                "suggestion": "联系银行处理。",
            },
        ],
    }
    narrator = _Narrator(override=forged)
    client = TestClient(create_app(replace(deps, bill_narrator=narrator)))
    body = client.get("/api/bills/analysis").json()

    assert body["narrative"]["degraded"] is True
    assert body["narrative"]["channel"] == "deterministic"
    assert "证据校验" in body["narrative"]["degrade_reason"]
    assert body["confirmed_saving_cents"] == 0


def test_narrative_with_invented_amount_or_transaction_is_rejected(deps: AppDeps) -> None:
    narrator = _Narrator(override={
        "summary": "本期账单存在需要核对的扣费项。",
        "findings": [
            {
                "finding_id": "abnormal-spend-01",
                "explanation": "该笔支出比常态多 3000 元，交易 txn_fake_001 可证明。",
                "suggestion": "联系银行处理。",
            },
        ],
    })
    client = TestClient(create_app(replace(deps, bill_narrator=narrator)))
    body = client.get("/api/bills/analysis").json()
    assert body["narrative"]["degraded"] is True
    assert "证据校验" in body["narrative"]["degrade_reason"]


def test_malicious_narrative_id_is_not_returned_by_api(deps: AppDeps) -> None:
    claim = "账户余额0元，退款已到账"
    narrator = _Narrator(override={
        "summary_id": claim,
        "findings": [],
    })
    client = TestClient(create_app(replace(deps, bill_narrator=narrator)))

    response = client.get("/api/bills/analysis")
    body = response.json()

    assert response.status_code == 200
    assert body["narrative"]["degraded"] is True
    assert body["narrative"]["degrade_reason"] == (
        "模型输出未通过证据校验（invalid_narrative）"
    )
    assert claim not in json.dumps(body, ensure_ascii=False)


def test_completed_bill_attaches_findings_and_other_intents_do_not(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))
    completed = _confirm(client, _submit(client, "查一下上月账单"))
    assert completed["state"] == "completed"
    assert completed["bill_findings"]["period"] == "2026-09"
    assert completed["bill_findings"]["data_source_is_demo"] is True
    summary = completed["bill_findings"]["summary"]
    assert summary["confirmed_count"] == sum(
        1 for item in completed["bill_findings"]["findings"] if item["status"] == "confirmed"
    )
    assert summary["suspected_count"] == sum(
        1 for item in completed["bill_findings"]["findings"] if item["status"] == "suspected"
    )

    balance = _confirm(client, _submit(client, "查余额"))
    assert balance["state"] == "completed"
    assert balance["bill_findings"] is None
    assert deps.audit.verify_chain() is True


def test_risk_leads_merge_multi_tag_and_exclude_subscription_info(
    deps: AppDeps,
) -> None:
    """可疑清单只收风险类命中；¥6,880 双标签合并，涨价归信息型线索。"""
    body = TestClient(create_app(deps)).get(
        "/api/bills/analysis?year=2026&month=10"
    ).json()
    assert body["summary"]["lead_count"] == 2
    assert body["summary"]["key_lead_count"] == 1
    kinds = [lead["kinds"] for lead in body["leads"]]
    assert ["abnormal-spend", "remote-city"] in kinds
    assert all("price-change" not in lead_kinds for lead_kinds in kinds)
    assert all("recurring-subscription" not in lead_kinds for lead_kinds in kinds)
    merged = next(
        lead for lead in body["leads"]
        if "abnormal-spend" in lead["kinds"] and "remote-city" in lead["kinds"]
    )
    assert merged["impact_cents"] == 688000
    assert len(merged["evidence"]) == 1

    price = body["price_changes"][0]
    assert price["status"] == "suspected"
    assert price["impact_cents"] == 200
    assert price["annualized_impact_cents"] == 2400
    assert len(price["evidence"]) == 2
    assert "涨幅 20.0%" in price["detail"]
    assert "未来 12 个月" in price["impact_basis"]
