import json
from datetime import date

from ai_banking_copilot.gateway.models import Payee
from ai_banking_copilot.planning.kernel import Kernel
from ai_banking_copilot.planning.llm import (
    FallbackIntentModel,
    Intent,
    IntentModel,
    IntentProposal,
    LlmIntentModel,
    LlmUnavailableError,
    TemplateIntentModel,
)

TODAY = date(2026, 10, 1)


class _BrokenIntentModel:
    """模拟上游 LLM 不可用。"""

    def propose(self, utterance: str, context: object) -> IntentProposal:
        raise LlmUnavailableError("上游不可用")


class _LowConfidenceIntentModel:
    def propose(self, utterance: str, context: object) -> IntentProposal:
        return IntentProposal(
            intents=(Intent(action="transfer.create", confidence=0.1, evidence="maybe"),),
            raw=utterance,
        )


class _SpanForgingIntentModel:
    """伪造解析片段：片段不在用户原文里，必须被作废。"""

    def propose(self, utterance: str, context: object) -> IntentProposal:
        return IntentProposal(
            intents=(
                Intent(
                    action="transfer.create",
                    slots={"span": "转给李娜 9999 元"},
                    confidence=0.9,
                    evidence="伪造片段",
                ),
            ),
            raw=utterance,
        )


def _context(payees: tuple[Payee, ...]) -> dict[str, object]:
    return {
        "today": TODAY,
        "payees": payees,
        "selected": {"user_id": "u_demo", "account": "acc_savings"},
    }


def test_unknown_utterance_is_safely_blocked(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    kernel = Kernel(TemplateIntentModel(), stub_registry)  # type: ignore[arg-type]
    result = kernel.plan("u_demo", "帮我做点什么", _context(payees))
    assert result.blocked is True
    assert result.plan.nodes == ()
    assert "安全拒绝" in result.rationale


def test_negated_instruction_does_not_produce_a_node(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    kernel = Kernel(TemplateIntentModel(), stub_registry)  # type: ignore[arg-type]
    result = kernel.plan("u_demo", "不要转给李娜 500 元", _context(payees))
    assert result.plan.nodes == ()
    assert result.blocked is True


def test_low_confidence_intent_is_dropped(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    kernel = Kernel(_LowConfidenceIntentModel(), stub_registry)  # type: ignore[arg-type]
    result = kernel.plan("u_demo", "转给李娜 500 元", _context(payees))
    assert result.plan.nodes == ()
    assert result.intents == ()
    assert "低置信度" in result.rationale


def test_forged_span_is_not_trusted(payees: tuple[Payee, ...], stub_registry: object) -> None:
    kernel = Kernel(_SpanForgingIntentModel(), stub_registry)  # type: ignore[arg-type]
    result = kernel.plan("u_demo", "查一下上月账单", _context(payees))
    assert result.plan.nodes == ()
    assert [question.slot for question in result.questions] == ["payee", "amount"]


def test_fallback_model_degrades_when_primary_is_unavailable() -> None:
    model = FallbackIntentModel(_BrokenIntentModel())
    proposal = model.propose("转给李娜 500 元", {})
    assert [intent.action for intent in proposal.intents] == ["transfer.create"]
    assert model.degraded is True


def test_fallback_model_stays_healthy_when_primary_works() -> None:
    def _transport(request: str) -> str:
        return json.dumps({"intents": [{"action": "bills.monthly", "confidence": 0.7}]})

    model = FallbackIntentModel(LlmIntentModel(transport=_transport))
    proposal = model.propose("看下账单", {})
    assert [intent.action for intent in proposal.intents] == ["bills.monthly"]
    assert model.degraded is False


def test_kernel_uses_the_fallback_channel_end_to_end(
    payees: tuple[Payee, ...], stub_registry: object
) -> None:
    model = FallbackIntentModel(_BrokenIntentModel())
    kernel = Kernel(model, stub_registry)  # type: ignore[arg-type]
    result = kernel.plan("u_demo", "转给李娜 500 元", _context(payees))
    assert [node.action for node in result.plan.nodes] == ["transfer.create"]
    assert result.blocked is False


def test_fallback_model_satisfies_the_intent_model_protocol() -> None:
    assert isinstance(FallbackIntentModel(TemplateIntentModel()), IntentModel)
