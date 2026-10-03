"""风险测评事实层：幂等补表/补行、缺行→None、出处校验（全程离线）。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.risk_store import (
    RISK_SCHEMA_SQL,
    RiskAssessmentRecord,
    StoreRiskAssessmentSource,
    init_risk_schema,
    risk_assessment,
    seed_risk_assessment,
    verify_risk,
)
from ai_banking_copilot.gateway.seed import DEMO_USER_ID, seed_database
from ai_banking_copilot.gateway.store import Store, StoreMode, init_schema


def _prepared(tmp_path: Path) -> Path:
    path = tmp_path / "risk.db"
    store = Store(path)
    init_schema(store)
    seed_database(store)
    store.close()
    return path


def test_init_and_seed_are_idempotent_on_existing_db(tmp_path: Path) -> None:
    path = _prepared(tmp_path)
    write = Store(path)
    init_risk_schema(write)
    seed_risk_assessment(write)
    init_risk_schema(write)
    seed_risk_assessment(write)
    rows = write.query("SELECT user_id FROM risk_assessments")
    write.close()
    assert len(rows) == 1


def test_risk_assessment_reads_seeded_row(tmp_path: Path) -> None:
    path = _prepared(tmp_path)
    write = Store(path)
    init_risk_schema(write)
    seed_risk_assessment(write)
    write.close()
    reader = Store(path, StoreMode.READ_ONLY)
    record = risk_assessment(reader, DEMO_USER_ID)
    reader.close()
    assert record is not None
    assert record.payload == RiskAssessmentRecord(
        user_id=DEMO_USER_ID,
        risk_profile="R3",
        assessed_on=DEFAULT_BOOKING_DATE - timedelta(days=30),
        valid_days=365,
    )


def test_missing_user_returns_none(tmp_path: Path) -> None:
    path = _prepared(tmp_path)
    write = Store(path)
    init_risk_schema(write)
    seed_risk_assessment(write)
    write.close()
    reader = Store(path, StoreMode.READ_ONLY)
    assert risk_assessment(reader, "u_missing") is None
    reader.close()


def test_verify_risk_true_then_false_on_tamper(tmp_path: Path) -> None:
    path = _prepared(tmp_path)
    write = Store(path)
    init_risk_schema(write)
    seed_risk_assessment(write)
    reader = Store(path, StoreMode.READ_ONLY)
    record = risk_assessment(reader, DEMO_USER_ID)
    assert record is not None
    assert verify_risk(reader, record.record_id, record.content_hash) is True
    reader.close()
    with write.transaction() as conn:
        conn.execute(
            "UPDATE risk_assessments SET risk_profile = 'R5' WHERE user_id = ?",
            (DEMO_USER_ID,),
        )
    reader2 = Store(path, StoreMode.READ_ONLY)
    assert verify_risk(reader2, record.record_id, record.content_hash) is False
    reader2.close()
    write.close()


def test_store_source_returns_payload_object(tmp_path: Path) -> None:
    path = _prepared(tmp_path)
    write = Store(path)
    init_risk_schema(write)
    seed_risk_assessment(write)
    write.close()
    reader = Store(path, StoreMode.READ_ONLY)
    source = StoreRiskAssessmentSource(reader)
    payload = source.assessment_for(DEMO_USER_ID)
    assert isinstance(payload, RiskAssessmentRecord)
    assert payload.risk_profile == "R3"
    assert source.assessment_for("u_missing") is None
    reader.close()


def test_schema_constant_declares_risk_table() -> None:
    assert "CREATE TABLE IF NOT EXISTS risk_assessments" in RISK_SCHEMA_SQL
