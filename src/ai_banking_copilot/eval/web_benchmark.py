"""模板通道的自建 Web 回归集：真实 HTTP 路由与独立 SQLite 预期。

运行证据保留在输出目录，每次运行、每条用例均独立建库。结果只代表这些
固定演示样本，不代表真实模型泛化、生产资金或未知攻击的阻断能力。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import time
import uuid
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from ai_banking_copilot.compose import build_deps
from ai_banking_copilot.gateway.core_bank import CoreBank
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.gateway.store import Store, StoreMode
from ai_banking_copilot.web.app import AppDeps, create_app

Json = dict[str, Any]
DATASET = Path(__file__).with_name("data") / "web_cases.json"
BASE_DATE = date(2026, 10, 1)
_NUMBER = re.compile(r"(?P<value>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>元|%)")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def load_cases(path: Path = DATASET) -> list[Json]:
    """仅加载自建固定回归样本；没有隐含的默认选定对象。"""
    cases: list[Json] = json.loads(path.read_text(encoding="utf-8"))
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Web 回归用例 id 重复")
    return cases


def _ratio(numerator: int, denominator: int) -> Json:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def summarize(results: list[Json]) -> Json:
    """误拒和任务失败分别计数；任一业务/出处失败都会保留在任务成功率内。"""
    legitimate = [item for item in results if item["kind"] == "legitimate"]
    attacks = [item for item in results if item["kind"] == "attack"]
    proofs = [proof for item in legitimate for proof in item["numeric_proofs"]]
    scenarios = sorted({item["scenario"] for item in legitimate})
    return {
        "attack_block_rate": _ratio(sum(item["passed"] for item in attacks), len(attacks)),
        "false_rejection_rate": _ratio(
            sum(item["false_rejection"] for item in legitimate), len(legitimate)
        ),
        "task_success_rate": _ratio(sum(item["passed"] for item in legitimate), len(legitimate)),
        "money_trace_rate": _ratio(sum(proof["passed"] for proof in proofs), len(proofs)),
        "scenario_task_success_rates": {
            scenario: _ratio(
                sum(item["passed"] for item in legitimate if item["scenario"] == scenario),
                sum(item["scenario"] == scenario for item in legitimate),
            )
            for scenario in scenarios
        },
    }


def _snapshot(store: Store) -> Json:
    """覆盖所有业务表，攻击前后比较实际行，包含幂等回执。"""
    tables = store.query(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%breaker%' ORDER BY name"
    )
    return {
        str(row["name"]): sorted(
            [list(item) for item in store.query(f'SELECT * FROM "{row["name"]}"')],
            key=repr,
        )
        for row in tables
    }


def _business_snapshot(store: Store) -> Json:
    """业务状态快照：保留行内容和数量，去除每次执行随机生成的技术 ID。"""
    tables = store.query(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%breaker%' ORDER BY name"
    )
    ignored = {"idempotency_key", "receipt_json", "event_id", "entry_hash", "prev_hash"}
    snapshot: Json = {}
    for table in tables:
        name = str(table["name"])
        rows = store.query(f'SELECT * FROM "{name}"')
        if not rows:
            snapshot[name] = []
            continue
        columns = [
            str(column)
            for column in rows[0].keys()
            if str(column) not in ignored and not str(column).endswith("_id")
        ]
        snapshot[name] = sorted(
            [[row[column] for column in columns] for row in rows], key=repr
        )
    return snapshot


def _prepare(case: Json, path: Path) -> str | None:
    store = Store(path)
    try:
        core = CoreBank(store)
        if case.get("prepare") == "hold":
            receipt = core.freeze_funds(
                account_id="acc_savings",
                amount=Money(50000),
                reason="回归准备",
                idempotency_key="web-regression-hold",
            )
            return receipt.record_ids[0].partition(":")[2]
        if case.get("prepare") == "schedule":
            core.book_transfer(
                user_id=DEMO_USER_ID,
                account_id="acc_savings",
                payee_id="p_lina",
                amount=Money(50000),
                booked_on=BASE_DATE,
                remark="回归准备",
                idempotency_key="web-regression-schedule",
            )
    finally:
        store.close()
    return None


def _provenance(deps: AppDeps) -> dict[str, list[str]]:
    hashes: dict[str, list[str]] = {}
    for event in deps.audit.events():
        if event.kind == "execution":
            for item in cast(list[Json], event.payload.get("provenance", [])):
                hashes.setdefault(item["record_id"], []).append(item["content_hash"])
    return hashes


def _verify_source(gateway: FactGateway, record_id: str, hashes: dict[str, list[str]]) -> bool:
    if record_id not in hashes:
        return False
    if record_id.startswith("bills:"):
        _, user_id, period = record_id.split(":")
        year, month = map(int, period.split("-"))
        current = gateway.monthly_bill(user_id, year, month).content_hash
        return current in hashes[record_id]
    return any(gateway.verify(record_id, value) for value in hashes[record_id])


def _numeric_candidates(
    rule: Json, store: Store, deps: AppDeps, hashes: dict[str, list[str]]
) -> list[tuple[Decimal, list[Json]]]:
    rows = [(str(row[0]), Decimal(str(row[1]))) for row in store.query(rule["sql"])]
    factor = Decimal(100) if rule.get("unit") == "cents" else Decimal(1)
    sources = [
        {
            "record_id": record_id,
            "raw_value": str(value),
            "hash_verified": _verify_source(deps.gateway, record_id, hashes),
        }
        for record_id, value in rows
    ]
    operation = rule.get("operation")
    if operation == "sum":
        return [(sum((value for _, value in rows), Decimal(0)) / factor, sources)]
    if operation == "yoy":
        year = rule["current_year"]
        current = sum((value for record_id, value in rows if f":{year}-" in record_id), Decimal(0))
        previous = sum(
            (value for record_id, value in rows if f":{year - 1}-" in record_id), Decimal(0)
        )
        percent = ((current - previous) / previous * 100).quantize(Decimal("0.01"))
        return [(percent, sources)]
    return [(value / factor, [source]) for (_, value), source in zip(rows, sources, strict=True)]


def _numeric_proofs(texts: list[str], case: Json, store: Store, deps: AppDeps) -> list[Json]:
    """每一个展示金额/百分比都必须同时匹配数值与回读哈希；漏声明也记失败。"""
    rules = case.get("numbers", [])
    hashes = _provenance(deps)
    proofs: list[Json] = []
    for text in texts:
        for token in _NUMBER.finditer(text):
            proof: Json = {
                "displayed": token.group(0),
                "context": text,
                "passed": False,
                "sources": [],
                "failure": "未声明该数字的独立取数规则",
            }
            for rule in rules:
                covered = any(
                    match.span("value") == token.span("value")
                    for match in re.finditer(rule["pattern"], text)
                )
                if not covered:
                    continue
                proof["failure"] = "数值或来源哈希与数据库回读不一致"
                try:
                    for value, sources in _numeric_candidates(rule, store, deps, hashes):
                        if Decimal(token.group("value")) != value:
                            continue
                        proof["sources"].extend(sources)
                        proof["computed_value"] = str(value)
                        if sources and all(source["hash_verified"] for source in sources):
                            proof["passed"] = True
                            proof["failure"] = ""
                except Exception as error:
                    proof["failure"] = f"出处回读失败：{type(error).__name__}: {error}"
            proofs.append(proof)
    return proofs


def _post(client: TestClient, trace: list[Json], endpoint: str, body: Json) -> Json:
    response = client.post(endpoint, json=body)
    payload: Json = response.json()
    trace.append(
        {
            "endpoint": endpoint,
            "http_status": response.status_code,
            "state": payload.get("state"),
            "message": payload.get("message", payload.get("detail", "")),
        }
    )
    return {**payload, "http_status": response.status_code}


def _confirm(client: TestClient, trace: list[Json], card: Json, session_id: str) -> Json:
    body = {
        "session_id": session_id,
        "card_id": card["card_id"],
        "frozen_hash": card["frozen_hash"],
    }
    if "strong_verify" in card["requires"]:
        challenge = _post(client, trace, "/api/verify/request", {"session_id": session_id})
        body["verification_code"] = challenge["code"]
    return _post(client, trace, "/api/confirm", body)


def _run_case(case: Json, case_dir: Path) -> Json:
    started = time.perf_counter()
    result: Json = {
        "id": case["id"],
        "scenario": case["scenario"],
        "kind": case["kind"],
        "passed": False,
        "state": None,
        "false_rejection": False,
        "failures": [],
        "checks": [],
        "numeric_proofs": [],
        "trace": [],
    }
    db_path = case_dir / "demo.db"
    store: Store | None = None
    resources = ExitStack()
    try:
        deps = build_deps(
            db_path,
            env={
                "AIB_DISABLE_LLM": "1",
                "AIB_AUDIT_KEY": "web-regression-local-key",
                "AIB_BREAKER_STATE": "memory",
            },
            resources=resources,
        )
        hold_id = _prepare(case, db_path)
        store = Store(db_path, StoreMode.READ_ONLY)
        initial = _snapshot(store)
        result["initial_state_sha256"] = _digest(initial)
        session_id = "regression"
        trace = result["trace"]
        texts: list[str] = []
        with TestClient(create_app(deps)) as client:
            selected = dict(case.get("selected", {}))
            if case.get("select_hold"):
                selected["hold"] = hold_id
            endpoint = case.get("endpoint", "/api/submit")
            body: Json = {"session_id": session_id}
            if endpoint == "/api/submit":
                body.update(utterance=case["utterance"], selected=selected)
            submitted = _post(client, trace, endpoint, body)
            if case.get("expect_clarification") and submitted.get("state") != "needs_clarification":
                result["failures"].append("未进入预期的澄清流程")
            if submitted.get("state") == "needs_clarification" and "answers" in case:
                submitted = _post(
                    client,
                    trace,
                    "/api/clarify",
                    {
                        "session_id": session_id,
                        "answers": case["answers"],
                    },
                )
            card = submitted.get("card")
            final = submitted
            if case["kind"] == "attack":
                final = _attack(case, client, trace, submitted, store, initial)
                if not final["safe"]:
                    result["failures"].append("授权负例未被阻断，或产生了非预期业务副作用")
            elif card:
                texts.extend(card["lines"])
                if case.get("expected_level") and card["level"] != case["expected_level"]:
                    result["failures"].append("计划卡授权级别与预期不同")
                final = _confirm(client, trace, card, session_id)
                texts.extend(item["message"] for item in final.get("results", []))
                if not final.get("results"):
                    result["failures"].append("执行响应未包含任何节点结果")
                if any(
                    item["status"] in {"failed", "unsupported", "skipped"}
                    for item in final.get("results", [])
                ):
                    result["failures"].append("执行结果包含失败或未完成节点")
                if case.get("repeat_due") and final.get("state") == "completed":
                    before_repeat = _snapshot(store)
                    repeated = _post(client, trace, "/api/due", {"session_id": "another-page"})
                    if repeated.get("card"):
                        _confirm(client, trace, repeated["card"], "another-page")
                    if _snapshot(store) != before_repeat:
                        result["failures"].append("新页面重复检查到期产生重复副作用")
            result["state"] = final.get("state")
            if case["kind"] == "legitimate":
                result["false_rejection"] = final.get("state") in {
                    "blocked",
                    "taken_over",
                } or final.get("http_status") in {403, 409}
                if final.get("state") != "completed":
                    result["failures"].append("业务未完成：" + str(final.get("message", final)))
                result["numeric_proofs"] = _numeric_proofs(texts, case, store, deps)
                if any(not proof["passed"] for proof in result["numeric_proofs"]):
                    result["failures"].append("存在无法同时验证出处和值的展示数字")
            for check in case.get("checks", []):
                rows = store.query(check["sql"])
                actual = rows[0][0] if rows else None
                passed = actual == check["expected"]
                result["checks"].append({**check, "actual": actual, "passed": passed})
                if not passed:
                    result["failures"].append(
                        f"{check['name']}：{actual!r} != {check['expected']!r}"
                    )
            if not deps.audit.verify_chain():
                result["failures"].append("审计链校验失败")
            result["audit_chain_verified"] = deps.audit.verify_chain()
            result["final_state_sha256"] = _digest(_business_snapshot(store))
            result["passed"] = not result["failures"]
    except Exception as error:
        result["failures"].append(f"{type(error).__name__}: {error}")
    finally:
        if store is not None:
            store.close()
        resources.close()
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    return result


def _attack(
    case: Json,
    client: TestClient,
    trace: list[Json],
    submitted: Json,
    store: Store,
    initial: Json,
) -> Json:
    attack = case["attack"]
    card = submitted.get("card")
    if attack == "injection":
        return {
            **submitted,
            "safe": submitted.get("state") in {"blocked", "taken_over"}
            and _snapshot(store) == initial,
        }
    if not card or submitted.get("state") != "awaiting_confirm":
        return {**submitted, "safe": False}
    if _snapshot(store) != initial:
        return {**submitted, "safe": False}
    if attack == "no_confirmation":
        return {**submitted, "safe": _snapshot(store) == initial}
    body = {
        "session_id": "regression",
        "card_id": card["card_id"],
        "frozen_hash": card["frozen_hash"],
    }
    if attack == "tampered_hash":
        body["frozen_hash"] = "0" * 64
    elif attack == "wrong_code":
        challenge = _post(client, trace, "/api/verify/request", {"session_id": "regression"})
        body["verification_code"] = "000001" if challenge.get("code") == "000000" else "000000"
    elif attack == "replay":
        original = _confirm(client, trace, card, "regression")
        if original.get("state") != "completed":
            return {**original, "safe": False}
        initial = _snapshot(store)
    final = _post(client, trace, "/api/confirm", body)
    return {**final, "safe": final["http_status"] == 409 and _snapshot(store) == initial}


def _metadata(cases: list[Json]) -> Json:
    try:
        package_version = version("ai-banking-copilot")
    except PackageNotFoundError:
        package_version = "未安装包元数据"
    repo = Path(__file__).resolve().parents[3]
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    files = sorted((repo / "src").rglob("*.py"))
    return {
        "package_version": package_version,
        "git_revision": revision,
        "git_dirty": bool(dirty),
        "source_sha256": _digest(
            {
                str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in files
            }
        ),
        "dataset_sha256": _digest(cases),
        "base_date": BASE_DATE.isoformat(),
        "channel": "template",
        "dataset_kind": "self_authored_regression",
        "scope": "固定演示种子与模板通道；自建回归，非盲测，非真实模型泛化成绩",
        "numeric_scope": "计划卡与执行结果中的金额及百分比；逐个核对数值和执行审计出处哈希",
    }


def run_benchmark(output_dir: Path, *, cases: list[Json] | None = None) -> Json:
    """输出逐例 JSON、带分子分母的 CSV；异常与业务失败均使 passed 为 false。"""
    cases = load_cases() if cases is None else cases
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = output_dir / "runs" / uuid.uuid4().hex
    results = [_run_case(case, run_dir / case["id"]) for case in cases]
    business = [
        {
            "id": item["id"],
            "passed": item["passed"],
            "state": item["state"],
            "final_state_sha256": item.get("final_state_sha256", ""),
            "checks": [
                {"name": check["name"], "actual": check["actual"], "passed": check["passed"]}
                for check in item["checks"]
            ],
            "numeric_proofs": [
                {"displayed": proof["displayed"], "passed": proof["passed"]}
                for proof in item["numeric_proofs"]
            ],
        }
        for item in results
    ]
    report: Json = {
        "metadata": _metadata(cases),
        "passed": bool(results) and all(item["passed"] for item in results),
        "business_digest": _digest(business),
        "evidence_directory": str(run_dir.resolve()),
        "summary": summarize(results),
        "cases": results,
    }
    (output_dir / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (output_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "metric",
                "numerator",
                "denominator",
                "rate",
                "package_version",
                "git_revision",
                "source_sha256",
                "dataset_sha256",
                "base_date",
                "channel",
                "dataset_kind",
                "failed_case_ids",
                "failure_reasons",
            ]
        )
        metrics = {
            key: value
            for key, value in report["summary"].items()
            if key != "scenario_task_success_rates"
        }
        metrics.update(
            {
                f"task_success:{key}": value
                for key, value in report["summary"]["scenario_task_success_rates"].items()
            }
        )
        failed_ids = ";".join(item["id"] for item in results if not item["passed"])
        failures = ";".join(
            f"{item['id']}: {' / '.join(item['failures'])}"
            for item in results
            if not item["passed"]
        )
        for name, metric in metrics.items():
            writer.writerow(
                [
                    name,
                    metric["numerator"],
                    metric["denominator"],
                    metric["rate"] if metric["rate"] is not None else "N/A",
                    report["metadata"]["package_version"],
                    report["metadata"]["git_revision"],
                    report["metadata"]["source_sha256"],
                    report["metadata"]["dataset_sha256"],
                    BASE_DATE.isoformat(),
                    "template",
                    "self_authored_regression",
                    failed_ids,
                    failures,
                ]
            )
    return report
