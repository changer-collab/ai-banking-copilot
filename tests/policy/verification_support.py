from __future__ import annotations

from datetime import date

from ai_banking_copilot.contracts.authorization import Decision, Level
from ai_banking_copilot.contracts.plan import Plan
from ai_banking_copilot.contracts.taint import Value
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.policy.execution import ExecutionContext
from ai_banking_copilot.policy.pep import Pep, _plan_hash
from ai_banking_copilot.policy.verification import (
    HmacStrongVerificationProofVerifier,
    StrongVerificationProof,
    VerificationBinding,
    create_challenge,
    verify_challenge,
)
from ai_banking_copilot.tools.registry import ToolCall

TEST_VERIFICATION_KEY = b"policy-test-strong-verification-key-v1"


def make_test_strong_verification_proof(
    *,
    user_id: str,
    session_id: str,
    execution_id: str,
    card_id: str,
    plan_hash: str,
) -> StrongVerificationProof:
    binding = VerificationBinding(user_id, session_id, execution_id, card_id, plan_hash)
    challenge, code = create_challenge(binding, key=TEST_VERIFICATION_KEY)
    consumed = verify_challenge(
        challenge, binding, code, key=TEST_VERIFICATION_KEY,
    )
    return HmacStrongVerificationProofVerifier(TEST_VERIFICATION_KEY).issue(consumed)


def issue_policy_write_grant(
    pep: Pep,
    call: ToolCall,
    decision: Decision,
    plan: Plan,
    *,
    user_id: str,
    session_id: str,
    execution_id: str,
    approved_level: Level | None = None,
    today: date = DEFAULT_BOOKING_DATE,
    completed_records: tuple[str, ...] = (),
    flags: frozenset[str] = frozenset(),
) -> ExecutionContext:
    level = decision.level if approved_level is None else approved_level
    raw_card_id = call.params.get("card_id", "")
    card_id = raw_card_id.raw if isinstance(raw_card_id, Value) else raw_card_id
    if not isinstance(card_id, str):
        card_id = ""
    proof = (
        make_test_strong_verification_proof(
            user_id=user_id,
            session_id=session_id,
            execution_id=execution_id,
            card_id=card_id,
            plan_hash=_plan_hash(plan),
        )
        if level >= Level.RED else None
    )
    return pep.issue_write_grant(
        call,
        decision,
        plan,
        user_id=user_id,
        session_id=session_id,
        execution_id=execution_id,
        approved_level=level,
        verification_proof=proof,
        card_id=card_id,
        strong_verified=level >= Level.RED,
        today=today,
        completed_records=completed_records,
        flags=flags,
    )
