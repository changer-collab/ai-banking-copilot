"""交互指标测量：把「3 步 → 1 句」从话术变成可复现的数字。

三类指标（全部从评测产物回读，不手写）：
1. **对话轮数**：完成一件事需要用户说几句话（`/api/submit` + `/api/clarify` 计为用户轮）。
2. **通道差分**：同一数据集在模板通道与严格模型通道下的轮数/成功率差异。
3. **二次澄清**：有多少任务需要用户第二次开口（缺槽追问）。
4. **错误恢复**：出错（4xx 或拒绝/接管/已提交未核实）后最终完成的比例；无样本输出 `null`。

另附「手机银行 N 步 vs 本产品 1 句」的对照：手机银行步数取自
`MOBILE_BANKING_STEPS`（打开 App → 找入口 → 选账户 → 选收款人 → 填金额 → 验证），
本产品句数取自评测 trace 的实际用户轮数，**不按理想值 1 硬写**。

用法：

    python scripts/measure_interaction.py --input runtime/benchmark/web-smoke-v2/run-1
    python scripts/measure_interaction.py --demo "转给李娜 500 元"
    python scripts/measure_interaction.py --input runtime/benchmark/web-smoke-v2/run-1 \\
        --compare runtime/benchmark-llm/web-smoke-v2/run-1
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

Json = dict[str, Any]

_USER_ENDPOINTS = frozenset({"/api/submit", "/api/clarify"})
_CONFIRM_ENDPOINTS = frozenset({"/api/confirm", "/api/due"})

_ERROR_HTTP_STATUS = 400
_FAILURE_STATES = frozenset({"rejected", "failed", "taken_over", "committed_unverified"})
"""只要 trace 里出现 4xx / 上述状态，就算这一例「出过错」；`committed_unverified` 计失败，
不美化成成功（口径见 `docs/2026-10-26/冲刺方案-2026-10-26.md` §6 第 6 项）。"""

MOBILE_BANKING_STEPS: dict[str, int] = {
    "transfer": 7,
    "bills": 5,
    "wealth": 6,
    "cards": 5,
    "subscriptions": 5,
    "cross": 9,
    "life_events": 8,
}
"""手机银行办同一件事的步数（打开 App → 入口 → 选对象 → 填金额 → 验证 → 确认）。

来源：现有 PWA 页面（`web/page.py`）的实际操作路径计数；`cross` / `life_events`
跨三个以上页面，故步数更高。改口径必须同时改这里与 `RESULTS.md` 交互一节。
"""

_SCENARIO_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("生日", "寿宴", "纪念日", "过节", "凑钱", "首付", "学费", "装修款"), "life_events"),
    (("卡丢了", "挂失", "虚拟卡", "限额", "锁卡"), "cards"),
    (("订阅", "代扣", "续费"), "subscriptions"),
    (("理财", "赎回", "申购"), "wealth"),
    (("账单", "消费", "异常交易"), "bills"),
    (("转账", "转给", "收款", "分摊"), "transfer"),
)


@dataclass(frozen=True)
class TurnStat:
    """单条用例的交互轮数。"""

    case_id: str
    scenario: str
    user_turns: int
    confirm_turns: int
    clarify_turns: int
    completed: bool
    error_events: int


def _load_report(directory: Path) -> Json:
    path = directory / "results.json"
    if not path.exists():
        raise SystemExit(f"找不到评测结果：{path}（先跑 scripts/run_benchmark.py）")
    return json.loads(path.read_text(encoding="utf-8"))


def _stats(report: Json, *, legitimate_only: bool = True) -> list[TurnStat]:
    """只统计「真办事」的用例：攻击 / 幂等 / 澄清组不进平均轮数。"""
    stats: list[TurnStat] = []
    for case in report.get("cases", []):
        if legitimate_only and case.get("metric_group") != "legitimate":
            continue
        trace: list[Json] = list(case.get("trace", []) or [])
        user_turns = sum(1 for item in trace if item.get("endpoint") in _USER_ENDPOINTS)
        clarify_turns = sum(1 for item in trace if item.get("endpoint") == "/api/clarify")
        confirm_turns = sum(1 for item in trace if item.get("endpoint") in _CONFIRM_ENDPOINTS)
        stats.append(
            TurnStat(
                case_id=str(case.get("id")),
                scenario=str(case.get("scenario")),
                user_turns=user_turns,
                confirm_turns=confirm_turns,
                clarify_turns=clarify_turns,
                completed=case.get("state") == "completed",
                error_events=sum(1 for item in trace if _is_error(item)),
            )
        )
    return stats


def _is_error(item: Json) -> bool:
    """trace 单步是否算「出错」：4xx 状态码，或明确的拒绝 / 失败 / 接管 / 已提交未核实。"""
    status = item.get("http_status")
    if isinstance(status, int) and status >= _ERROR_HTTP_STATUS:
        return True
    return item.get("state") in _FAILURE_STATES


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _summary(stats: Sequence[TurnStat]) -> Json:
    if not stats:
        return {"cases": 0}
    utterances = [item.user_turns for item in stats]
    errored = [item for item in stats if item.error_events > 0]
    recovery = _rate(sum(item.completed for item in errored), len(errored))
    return {
        "cases": len(stats),
        "mean_user_turns": round(mean(utterances), 3),
        "single_utterance_rate": round(
            _rate(sum(item.user_turns <= 1 for item in stats), len(stats)) or 0.0, 4
        ),
        "clarification_case_rate": round(
            _rate(sum(item.clarify_turns > 0 for item in stats), len(stats)) or 0.0, 4
        ),
        "mean_confirms": round(mean([item.confirm_turns for item in stats]), 3),
        "system_initiated_rate": round(
            _rate(sum(item.user_turns == 0 for item in stats), len(stats)) or 0.0, 4
        ),
        "completed_rate": round(_rate(sum(item.completed for item in stats), len(stats)) or 0.0, 4),
        # 无样本不写 0：0 会被误读成「恢复全失败」，null 表示这批产物里没有出错样本
        "recovery_rate": None if recovery is None else round(recovery, 4),
        "recovery_samples": len(errored),
    }


def _per_scenario(stats: Sequence[TurnStat]) -> Json:
    scenarios = sorted({item.scenario for item in stats})
    return {
        scenario: _summary([item for item in stats if item.scenario == scenario])
        for scenario in scenarios
    }


def _steps_vs_utterances(stats: Sequence[TurnStat]) -> Json:
    """同一件事：手机银行步数 / 本产品用户轮数（该场景实测均值，**剔除系统发起**）。"""
    rows: Json = {}
    for scenario in sorted({item.scenario for item in stats}):
        group = [item for item in stats if item.scenario == scenario and item.user_turns > 0]
        mobile = MOBILE_BANKING_STEPS.get(scenario)
        if not group:
            rows[scenario] = {
                "mobile_banking_steps": mobile,
                "product_user_turns": 0,
                "reduction": None,
                "note": "该场景用例均由系统发起（/api/due），无用户开口",
            }
            continue
        measured = round(mean([item.user_turns for item in group]), 3)
        rows[scenario] = {
            "mobile_banking_steps": mobile,
            "product_user_turns": measured,
            "reduction": None if not mobile else round(1 - measured / mobile, 4),
        }
    return rows


def _scenario_of_text(text: str) -> str | None:
    for keywords, scenario in _SCENARIO_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return scenario
    return None


def _demo_row(text: str) -> Json:
    scenario = _scenario_of_text(text)
    if scenario is None:
        return {"utterance": text, "scenario": None, "mobile_banking_steps": None}
    mobile = MOBILE_BANKING_STEPS[scenario]
    return {
        "utterance": text,
        "scenario": scenario,
        "mobile_banking_steps": mobile,
        "product_user_turns": 1,
        "product_confirms": 1,
        "reduction": round(1 - 1 / mobile, 4),
    }


def _pct(value: float | None) -> str:
    """无样本显示「null」，避免把「没样本」读成「0%」。"""
    return "null（本批产物无出错样本）" if value is None else f"{value:.2%}"


def _print_markdown(payload: Json) -> None:
    primary = payload["primary"]
    print("## 交互指标（实测）\n")
    print(f"- 用例数：{primary['cases']}")
    print(f"- 平均用户轮数：{primary['mean_user_turns']}")
    print(f"- 一句话完成率：{primary['single_utterance_rate']:.2%}")
    print(f"- 需二次澄清占比：{primary['clarification_case_rate']:.2%}")
    print(f"- 平均确认次数：{primary['mean_confirms']}")
    print(f"- 系统发起占比（`/api/due`，用户未开口）：{primary['system_initiated_rate']:.2%}")
    print(f"- 业务完成率：{primary['completed_rate']:.2%}")
    print(
        f"- 错误恢复成功率：{_pct(primary['recovery_rate'])}"
        f"（样本 {primary['recovery_samples']} 例，口径：出错后最终 completed；"
        "4xx / 拒绝 / 接管 / `committed_unverified` 均计出错）\n"
    )
    print("| 场景 | 手机银行步数 | 本产品用户轮数 | 步数降幅 |")
    print("|---|---|---|---|")
    for scenario, row in payload["steps_vs_utterances"].items():
        if row.get("note"):
            print(f"| {scenario} | {row['mobile_banking_steps']} | 0（系统发起） | — |")
            continue
        print(
            f"| {scenario} | {row['mobile_banking_steps']} | "
            f"{row['product_user_turns']} | {row['reduction']:.2%} |"
        )
    if "comparison" in payload:
        print("\n### 通道差分\n")
        print("| 通道 | 平均用户轮数 | 一句话完成率 | 业务完成率 |")
        print("|---|---|---|---|")
        for name, item in payload["comparison"].items():
            print(
                f"| {name} | {item['mean_user_turns']} | "
                f"{item['single_utterance_rate']:.2%} | {item['completed_rate']:.2%} |"
            )
    for row in payload.get("demos", []):
        if row["scenario"] is None:
            print(f"\n- 「{row['utterance']}」未匹配到已知场景，无法给出对照。")
            continue
        print(
            f"\n- 「{row['utterance']}」：手机银行 {row['mobile_banking_steps']} 步 → "
            f"本产品 {row['product_user_turns']} 句 + {row['product_confirms']} 次确认"
            f"（降幅 {row['reduction']:.2%}）"
        )


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="统计交互轮数并对照手机银行步数")
    parser.add_argument(
        "--input", type=Path, default=Path("runtime/benchmark/web-smoke-v2/run-1")
    )
    parser.add_argument(
        "--compare",
        type=Path,
        default=None,
        help="另一通道的产物目录（如 llm-strict），用于通道差分",
    )
    parser.add_argument("--demo", action="append", default=[], help="演示话术，可重复")
    parser.add_argument("--output", type=Path, default=None, help="把结果写成 JSON")
    args = parser.parse_args()

    stats = _stats(_load_report(args.input))
    payload: Json = {
        "primary": _summary(stats),
        "per_scenario": _per_scenario(stats),
        "steps_vs_utterances": _steps_vs_utterances(stats),
        "demos": [_demo_row(text) for text in args.demo],
    }
    if args.compare is not None:
        payload["comparison"] = {
            str(args.input.name): payload["primary"],
            str(args.compare.name): _summary(_stats(_load_report(args.compare))),
        }
    _print_markdown(payload)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n结果已写入：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
