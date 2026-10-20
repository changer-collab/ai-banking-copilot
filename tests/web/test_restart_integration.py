from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_banking_copilot.compose import build_deps
from ai_banking_copilot.policy.audit import KIND_EXECUTION, AuditEvent
from ai_banking_copilot.web.app import create_app
from ai_banking_copilot.web.session_repository import SessionRepository
from ai_banking_copilot.web.session_snapshot import SessionSnapshot

ENV = {"AIB_DISABLE_LLM": "1", "AIB_BREAKER_STATE": "memory"}


def test_restart_keeps_partial_results_but_requires_new_confirmation(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    with ExitStack() as resources:
        client = TestClient(create_app(build_deps(path, ENV, resources=resources)))
        submitted = client.post("/api/submit", json={
            "session_id": "restart", "utterance": "冻结500元，然后转给李娜300元",
            "selected": {"account": "acc_savings", "grant": "explicit"},
        }).json()
        card = submitted["card"]
        part = client.post("/api/confirm", json={
            "session_id": "restart", "card_id": card["card_id"],
            "frozen_hash": card["frozen_hash"], "steps": 1,
        })
        assert part.status_code == 200
        assert len(part.json()["results"]) == 1
    with ExitStack() as resources:
        client = TestClient(create_app(build_deps(path, ENV, resources=resources)))
        restored = client.get("/api/session", params={"session_id": "restart"})
        assert restored.status_code == 200
        assert restored.json()["state"] == "awaiting_confirm"
        assert restored.json()["card"]["card_id"] == card["card_id"]
        assert len(restored.json()["results"]) == 1
        assert "snapshot" not in restored.json()
        resumed = client.post("/api/resume", json={"session_id": "restart"})
        assert resumed.status_code == 409
        done = client.post("/api/confirm", json={
            "session_id": "restart", "card_id": card["card_id"],
            "frozen_hash": card["frozen_hash"],
        })
        assert done.status_code == 200
        assert done.json()["state"] == "completed"
        assert len(done.json()["results"]) == 2


def test_restart_invalidates_active_red_challenge(tmp_path: Path) -> None:
    path = tmp_path / "challenge.db"
    with ExitStack() as resources:
        client = TestClient(create_app(build_deps(path, ENV, resources=resources)))
        card = client.post("/api/submit", json={"utterance": "转给李娜1500元"}).json()["card"]
        old_code = client.post("/api/verify/request", json={}).json()["code"]
    with ExitStack() as resources:
        client = TestClient(create_app(build_deps(path, ENV, resources=resources)))
        failed = client.post("/api/confirm", json={
            "card_id": card["card_id"], "frozen_hash": card["frozen_hash"],
            "verification_code": old_code,
        })
        assert failed.status_code == 409
        new_code = client.post("/api/verify/request", json={}).json()["code"]
        done = client.post("/api/confirm", json={
            "card_id": card["card_id"], "frozen_hash": card["frozen_hash"],
            "verification_code": new_code,
        })
        assert done.status_code == 200
        assert done.json()["state"] == "completed"


class _FailingRepository:
    def __init__(self, base: SessionRepository, *, after_write: bool) -> None:
        self.base = base
        self.after_write = after_write

    def load(self, user_id: str, session_id: str) -> SessionSnapshot | None:
        return self.base.load(user_id, session_id)

    def save(
        self, snapshot: SessionSnapshot, *, expected_revision: int | None,
    ) -> SessionSnapshot:
        fail = bool(snapshot.payload["completed_node_ids"]) if self.after_write else (
            snapshot.payload["in_flight"] is not None
        )
        if fail:
            raise RuntimeError("injected snapshot failure")
        return self.base.save(snapshot, expected_revision=expected_revision)


@pytest.mark.parametrize("after_write", [False, True])
def test_checkpoint_failure_stops_writes_and_restart_reconciles(
    tmp_path: Path, after_write: bool,
) -> None:
    path = tmp_path / "checkpoint.db"
    with ExitStack() as resources:
        deps = build_deps(path, ENV, resources=resources)
        assert deps.session_repository is not None
        configured = replace(deps, session_repository=_FailingRepository(
            deps.session_repository, after_write=after_write,
        ))
        client = TestClient(create_app(configured))
        card = client.post("/api/submit", json={"utterance": "转给李娜200元"}).json()["card"]
        failed = client.post("/api/confirm", json={
            "card_id": card["card_id"], "frozen_hash": card["frozen_hash"],
        })
        assert failed.status_code == 409
        before_restart = deps.gateway.account("acc_savings").payload.balance.cents
        retried = client.post("/api/confirm", json={
            "card_id": card["card_id"], "frozen_hash": card["frozen_hash"],
        })
        assert retried.status_code == 409
        assert deps.gateway.account("acc_savings").payload.balance.cents == before_restart
    with ExitStack() as resources:
        deps = build_deps(path, ENV, resources=resources)
        client = TestClient(create_app(deps))
        restored = client.get("/api/session").json()
        assert restored["state"] == ("completed" if after_write else "awaiting_confirm")
        assert len(restored["results"]) == (1 if after_write else 0)
        done = client.post("/api/confirm", json={
            "card_id": card["card_id"], "frozen_hash": card["frozen_hash"],
        })
        assert done.status_code == 200
        assert done.json()["state"] == "completed"


def test_committed_without_audit_remains_blocked_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "unverified.db"
    with ExitStack() as resources:
        deps = build_deps(path, ENV, resources=resources)
        original = deps.audit.append

        def append(event: AuditEvent) -> str:
            if event.kind == KIND_EXECUTION:
                raise RuntimeError("injected execution audit failure")
            return original(event)

        monkeypatch.setattr(deps.audit, "append", append)
        client = TestClient(create_app(deps))
        card = client.post("/api/submit", json={"utterance": "转给李娜200元"}).json()["card"]
        first = client.post("/api/confirm", json={
            "card_id": card["card_id"], "frozen_hash": card["frozen_hash"],
        }).json()
        assert first["state"] == "interrupted"
        assert first["results"][0]["status"] == "committed_unverified"
    with ExitStack() as resources:
        deps = build_deps(path, ENV, resources=resources)
        client = TestClient(create_app(deps))
        restored = client.get("/api/session").json()
        assert restored["state"] == "interrupted"
        assert restored["results"][0]["status"] == "committed_unverified"
        assert client.post("/api/resume", json={}).status_code == 409
        assert client.post("/api/reverse", json={}).status_code == 409
        assert client.post("/api/confirm", json={
            "card_id": card["card_id"], "frozen_hash": card["frozen_hash"],
        }).status_code == 409
        assert client.post("/api/submit", json={"utterance": "查余额"}).status_code == 409


@pytest.mark.parametrize("utterance", ["转给李娜200元", "查余额"])
def test_stricter_review_after_restart_preserves_completed_state(
    tmp_path: Path, utterance: str,
) -> None:
    path = tmp_path / "terminal.db"
    with ExitStack() as resources:
        client = TestClient(create_app(build_deps(path, ENV, resources=resources)))
        card = client.post("/api/submit", json={"utterance": utterance}).json()["card"]
        done = client.post("/api/confirm", json={
            "card_id": card["card_id"], "frozen_hash": card["frozen_hash"],
        }).json()
        assert done["state"] == "completed"
    with ExitStack() as resources:
        deps = replace(build_deps(path, ENV, resources=resources), reviewer_required=True)
        client = TestClient(create_app(deps))
        restored = client.get("/api/session").json()
        assert restored["state"] == "completed"
        assert restored["record_ids"] == done["record_ids"]
