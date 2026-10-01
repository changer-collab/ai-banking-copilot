from ai_banking_copilot.contracts.authorization import Level
from ai_banking_copilot.eval.dataset import EvalCase
from ai_banking_copilot.eval.invariants import InvariantResult, check_invariants
from ai_banking_copilot.eval.pipeline import CaseResult, NodeOutcome


def _node(
    node_id: str,
    action: str,
    level: Level | None,
    *,
    pure: bool = False,
    outflow: bool = False,
    daily: int = 0,
    blocked_object: bool = False,
) -> NodeOutcome:
    return NodeOutcome(
        node_id=node_id,
        action=action,
        level=level,
        is_pure_query=pure,
        is_outflow=outflow,
        daily_accumulated_cents=daily,
        object_blocked=blocked_object,
        executed_status="skipped",
    )


def _case(case_id: str, level: str, blocked: bool, category: str = "transfer") -> EvalCase:
    return EvalCase(case_id, f"指令 {case_id}", level, blocked, category)


def _result(
    case: EvalCase,
    level: Level | None,
    nodes: tuple[NodeOutcome, ...] = (),
    *,
    net: int = 0,
    irreversibles: tuple[str, ...] = (),
    flags: frozenset[str] = frozenset(),
    blocked_object: bool = False,
    blocked: bool = False,
) -> CaseResult:
    return CaseResult(
        case=case,
        status="blocked" if blocked else "planned",
        blocked=blocked,
        level=level,
        nodes=nodes,
        net_amount_cents=net,
        irreversibles=irreversibles,
        context_flags=flags,
        blocked_object=blocked_object,
        derived_released=None,
        elapsed_ms=1,
        message="fixture",
    )


def _by_name(results: tuple[InvariantResult, ...]) -> dict[str, InvariantResult]:
    return {item.name: item for item in results}


def test_i1_prime_holds_for_outflow_within_limit() -> None:
    case = _case("c1", "YELLOW", False)
    results = (
        _result(
            case,
            Level.YELLOW,
            (_node("n1", "transfer.create", Level.YELLOW, outflow=True, daily=50000),),
        ),
    )
    assert _by_name(check_invariants((case,), results))["I1′ 金额序关系"].holds is True


def test_i1_prime_fails_when_over_limit_is_not_red() -> None:
    case = _case("c1", "YELLOW", False)
    results = (
        _result(
            case,
            Level.YELLOW,
            (_node("n1", "transfer.create", Level.YELLOW, outflow=True, daily=150000),),
        ),
    )
    assert _by_name(check_invariants((case,), results))["I1′ 金额序关系"].holds is False


def test_i1_prime_is_skipped_under_context_anomaly() -> None:
    case = _case("c1", "MANUAL", False)
    node = _node("n1", "transfer.create", Level.MANUAL, outflow=True, daily=50000)
    results = (_result(case, Level.MANUAL, (node,), flags=frozenset({"injection_detected"})),)
    assert _by_name(check_invariants((case,), results))["I1′ 金额序关系"].holds is True


def test_i2_prime_requires_deny_for_blocked_object() -> None:
    case = _case("c1", "DENY", False)
    node = _node(
        "n1", "transfer.create", Level.DENY, outflow=True, daily=30000, blocked_object=True
    )
    good = _result(case, Level.DENY, (node,), blocked_object=True)
    assert _by_name(check_invariants((case,), (good,)))["I2′ 对象序关系"].holds is True
    bad = _result(case, Level.YELLOW, (node,), blocked_object=True)
    assert _by_name(check_invariants((case,), (bad,)))["I2′ 对象序关系"].holds is False


def test_i3_prime_requires_red_for_irreversible() -> None:
    case = _case("c1", "RED", False, category="cards")
    red_node = _node("n1", "card.report_lost", Level.RED)
    yellow_node = _node("n1", "card.report_lost", Level.YELLOW)
    good = _result(case, Level.RED, (red_node,), irreversibles=("n1",))
    bad = _result(case, Level.YELLOW, (yellow_node,), irreversibles=("n1",))
    assert _by_name(check_invariants((case,), (good,)))["I3′ 可逆性序关系"].holds is True
    assert _by_name(check_invariants((case,), (bad,)))["I3′ 可逆性序关系"].holds is False


def test_i4_prime_net_effect_threshold() -> None:
    case = _case("c1", "RED", False)
    node = _node("n1", "transfer.create", Level.YELLOW, outflow=True, daily=80000)
    over = _result(case, Level.RED, (node,), net=150000)
    under = _result(case, Level.YELLOW, (node,), net=150000)
    assert _by_name(check_invariants((case,), (over,)))["I4′ 净效应序关系"].holds is True
    assert _by_name(check_invariants((case,), (under,)))["I4′ 净效应序关系"].holds is False


def test_i4_prime_zero_net_is_at_most_yellow() -> None:
    case = _case("c1", "GREEN", False, category="bills")
    node = _node("n1", "bills.monthly", Level.GREEN, pure=True)
    green = _result(case, Level.GREEN, (node,), net=0)
    red = _result(case, Level.RED, (node,), net=0)
    assert _by_name(check_invariants((case,), (green,)))["I4′ 净效应序关系"].holds is True
    assert _by_name(check_invariants((case,), (red,)))["I4′ 净效应序关系"].holds is False


def test_i4_prime_skips_non_currency_dag() -> None:
    case = _case("c1", "RED", False, category="cards")
    node = _node("n1", "card.report_lost", Level.RED)
    results = (_result(case, Level.RED, (node,), net=0, irreversibles=("n1",)),)
    assert _by_name(check_invariants((case,), results))["I4′ 净效应序关系"].holds is True


def test_green_purity_rejects_non_query_green_node() -> None:
    case = _case("c1", "GREEN", False)
    node = _node("n1", "transfer.create", Level.GREEN, pure=False, outflow=True, daily=100)
    results = (_result(case, Level.GREEN, (node,), net=100),)
    assert _by_name(check_invariants((case,), results))["绿色纯度"].holds is False


def test_check_invariants_returns_seven_named_invariants() -> None:
    case = _case("c1", "YELLOW", False)
    results = (
        _result(
            case,
            Level.YELLOW,
            (_node("n1", "transfer.create", Level.YELLOW, outflow=True, daily=50000),),
        ),
    )
    names = [item.name for item in check_invariants((case,), results)]
    assert names == [
        "I1′ 金额序关系",
        "I2′ 对象序关系",
        "I3′ 可逆性序关系",
        "I4′ 净效应序关系",
        "绿色纯度",
        "全局非退化",
        "must-pass 外部锚",
    ]
