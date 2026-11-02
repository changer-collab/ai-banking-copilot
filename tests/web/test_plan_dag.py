"""任务 DAG 投影与逐节点状态（2026-10-30）。

口径：依赖与状态**只由服务端给出**；前端不推导、不补节点。
"""

from pathlib import Path

from fastapi.testclient import TestClient

from ai_banking_copilot.compose import build_deps
from ai_banking_copilot.contracts.authorization import Decision, Level, ReasonCode
from ai_banking_copilot.contracts.plan import AnchorSource, IntentRef, Plan, PlanNode
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.web.app import AppDeps, create_app
from ai_banking_copilot.web.plan_card import plan_nodes_payload, render_plan_card


def _node(
    node_id: str,
    action: str,
    params: dict[str, object],
    depends_on: tuple[str, ...] = (),
) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        action=action,
        params=params,
        intent=IntentRef(source=AnchorSource.EXPLICIT, anchor_id=f"a-{node_id}"),
        depends_on=depends_on,
    )


def test_nodes_payload_keeps_mechanical_dependencies() -> None:
    plan = Plan(plan_id="p", nodes=(
        _node("n1", "transfer.create", {"account_id": "acc_savings", "amount_cents": 50000}),
        _node(
            "n2",
            "wealth.subscribe",
            {"account_id": "acc_savings", "amount_cents": 100000},
            ("n1",),
        ),
        _node("n3", "funds.release", {"hold_id": "hold_1"}),
    ))

    rows = plan_nodes_payload(plan)

    assert [row["node_id"] for row in rows] == ["n1", "n2", "n3"]
    assert [row["index"] for row in rows] == [1, 2, 3]
    assert rows[1]["depends_on"] == ["n1"]
    assert rows[1]["resource_label"] == "同一账户"
    assert rows[2]["resource_label"] == "同一笔冻结"
    assert rows[0]["label"] == "转账"


def test_nodes_payload_is_empty_without_plan() -> None:
    assert plan_nodes_payload(None) == []


def test_card_lock_lists_bound_subscriptions_as_impact(gateway: FactGateway) -> None:
    """挂失/锁卡前必须说清该卡上已有的代扣——关系来自库，不是推测。"""
    node = _node("n1", "card.lock", {"card_id": "card_debit"})
    plan = Plan(plan_id="p", nodes=(node,))

    card = render_plan_card(
        plan,
        Decision(Level.RED, (ReasonCode.IRREVERSIBLE,)),
        gateway,
        user_id=DEMO_USER_ID,
    )

    text = "；".join(card.impact)
    assert "3 个在扣订阅" in text
    assert "云盘会员" in text and "音乐会员" in text
    assert "不会自动取消商户侧代扣" in text


def test_impact_is_hidden_without_user_id(gateway: FactGateway) -> None:
    node = _node("n1", "card.lock", {"card_id": "card_debit"})
    card = render_plan_card(
        Plan(plan_id="p", nodes=(node,)),
        Decision(Level.RED, (ReasonCode.IRREVERSIBLE,)),
        gateway,
    )
    assert card.impact == ()


def test_due_plan_exposes_dependency_edges(tmp_path: Path) -> None:
    """到期待办（生日链）必须给出**至少一条依赖边**，且依赖只指向计划内节点。"""
    deps = build_deps(
        tmp_path / "dag.db",
        {"AIB_DISABLE_LLM": "1", "AIB_BREAKER_STATE": "memory", "AIB_AUDIT_KEY": "test"},
    )
    client = TestClient(create_app(deps))

    due = client.post("/api/due", json={"session_id": "dag"}).json()
    assert due["state"] == "awaiting_confirm", due.get("message")

    nodes = due["card"]["nodes"]
    assert len(nodes) >= 3, "生日链应至少三步"
    ids = {node["node_id"] for node in nodes}
    linked = [node for node in nodes if node["depends_on"]]
    assert linked, "生日链必须给出至少一条依赖边"
    for node in nodes:
        assert set(node["depends_on"]) <= ids


def test_http_card_exposes_dag_and_per_node_status(deps: AppDeps) -> None:
    client = TestClient(create_app(deps))

    submitted = client.post("/api/submit", json={"utterance": "转给李娜 500 元"}).json()
    card = submitted["card"]
    assert card["nodes"], "计划卡必须带上任务 DAG"
    assert card["nodes"][0]["action"] == "transfer.create"
    assert card["nodes"][0]["depends_on"] == []

    confirmed = client.post(
        "/api/confirm",
        json={"card_id": card["card_id"], "frozen_hash": card["frozen_hash"]},
    ).json()
    assert confirmed["state"] == "completed"
    assert [row["status"] for row in confirmed["node_progress"]] == ["completed"]
    assert confirmed["card"]["nodes"], "执行后仍要能看到同一份 DAG"

    reversed_ = client.post("/api/reverse", json={}).json()
    assert reversed_["state"] == "reversed"

    session = client.get("/api/session").json()
    assert [row["status"] for row in session["node_progress"]] == ["reversed"]
