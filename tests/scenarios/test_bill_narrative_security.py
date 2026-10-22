"""账单叙事的结构化事实约束与安全降级。"""

from collections.abc import Iterator, Mapping
from datetime import date
from pathlib import Path

import pytest

from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema
from ai_banking_copilot.scenarios.bill_findings import (
    NARRATIVE_SUMMARY_ID,
    NarrativeRejectedError,
    analyze_bill,
    narrate_bill_findings,
    narrative_choice_ids,
    validate_narrative,
)


class _Narrator:
    def __init__(self, payload: Mapping[str, object]) -> None:
        self.payload = payload

    def narrate(self, report: object) -> Mapping[str, object]:
        return self.payload


@pytest.fixture
def bill_store(tmp_path: Path) -> Iterator[tuple[Store, FactGateway]]:
    path = tmp_path / "bill_narrative_security.db"
    writer = Store(path)
    init_schema(writer)
    seed_database(writer)
    with writer.transaction() as conn:
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM subscriptions")
        conn.execute(
            "INSERT INTO transactions(txn_id, user_id, account_id, booked_on, amount_cents,"
            " merchant, category, channel, direction) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "fee-a", DEMO_USER_ID, "acc_current", "2026-09-02", -3000,
                "增值服务费", "购物", "app", "out",
            ),
        )
        conn.execute(
            "INSERT INTO transactions(txn_id, user_id, account_id, booked_on, amount_cents,"
            " merchant, category, channel, direction) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "fee-b", DEMO_USER_ID, "acc_current", "2026-09-03", -3000,
                "增值服务费", "购物", "app", "out",
            ),
        )
    reader = Store(path, StoreMode.READ_ONLY)
    yield writer, FactGateway(reader)
    reader.close()
    writer.close()


def _report(gateway: FactGateway):
    return analyze_bill(gateway, DEMO_USER_ID, 2026, 9, as_of=date(2026, 10, 1))


@pytest.mark.parametrize(
    "claim",
    [
        "余额充足",
        "转账成功",
        "退款已到账",
        "The balance is sufficient",
        "The transfer succeeded",
        "The refund has arrived",
    ],
)
def test_free_fact_claims_degrade_even_with_a_valid_finding_id(
    bill_store: tuple[Store, FactGateway], claim: str
) -> None:
    _, gateway = bill_store
    report = _report(gateway)
    finding = report.findings[0]
    narrative = narrate_bill_findings(
        report,
        _Narrator(
            {
                "summary_id": NARRATIVE_SUMMARY_ID,
                "findings": [
                    {
                        "finding_id": finding.finding_id,
                        "explanation": claim,
                        "suggestion": claim,
                    }
                ],
            }
        ),
    )

    assert narrative.degraded is True
    assert narrative.channel == "deterministic"
    assert claim not in narrative.findings[0].explanation


def test_registered_choices_render_server_facts(
    bill_store: tuple[Store, FactGateway],
) -> None:
    _, gateway = bill_store
    report = _report(gateway)
    finding = report.findings[0]
    choices = narrative_choice_ids(finding)
    narrative = narrate_bill_findings(
        report,
        _Narrator(
            {
                "summary_id": NARRATIVE_SUMMARY_ID,
                "findings": [
                    {
                        "finding_id": finding.finding_id,
                        "explanation_id": choices["explanation"][0],
                        "suggestion_id": choices["suggestion"][0],
                    }
                ],
            }
        ),
    )

    assert narrative.degraded is False
    assert narrative.findings[0].explanation == finding.detail
    assert narrative.findings[0].suggestion == finding.suggestion
    assert "增值服务费" in narrative.findings[0].explanation


def test_choice_from_another_finding_kind_is_rejected(
    bill_store: tuple[Store, FactGateway],
) -> None:
    _, gateway = bill_store
    report = _report(gateway)
    finding = report.findings[0]
    narrative = narrate_bill_findings(
        report,
        _Narrator(
            {
                "summary_id": NARRATIVE_SUMMARY_ID,
                "findings": [
                    {
                        "finding_id": finding.finding_id,
                        "explanation_id": "abnormal_spend_suspected_evidence",
                        "suggestion_id": narrative_choice_ids(finding)["suggestion"][0],
                    }
                ],
            }
        ),
    )

    assert narrative.degraded is True
    assert narrative.degrade_reason == "模型输出未通过证据校验（invalid_narrative）"


@pytest.mark.parametrize("field", ["summary_id", "finding_id", "explanation_id", "suggestion_id"])
def test_rejection_errors_do_not_echo_model_controlled_ids(
    bill_store: tuple[Store, FactGateway], field: str
) -> None:
    _, gateway = bill_store
    report = _report(gateway)
    finding = report.findings[0]
    choices = narrative_choice_ids(finding)
    claim = "账户余额0元，退款已到账"
    item = {
        "finding_id": finding.finding_id,
        "explanation_id": choices["explanation"][0],
        "suggestion_id": choices["suggestion"][0],
    }
    payload: dict[str, object] = {
        "summary_id": NARRATIVE_SUMMARY_ID,
        "findings": [item],
    }
    if field == "summary_id":
        payload[field] = claim
    else:
        item[field] = claim

    with pytest.raises(NarrativeRejectedError) as error:
        validate_narrative(report, payload)
    assert claim not in str(error.value)
    assert str(error.value) == NarrativeRejectedError.message

    narrative = narrate_bill_findings(report, _Narrator(payload))
    assert narrative.degraded is True
    assert narrative.degrade_reason == "模型输出未通过证据校验（invalid_narrative）"
    assert claim not in repr(narrative)
