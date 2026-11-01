"""服务页自检：对**真实运行的服务**校验内联页 / 静态资源 / 计划卡额度字段。

用法：
    # 终端 A：启动服务
    python -m ai_banking_copilot
    # 终端 B：跑自检（默认 http://127.0.0.1:8000）
    python scripts/check_served_page.py
    python scripts/check_served_page.py --base-url http://127.0.0.1:8000

校验：内联页结构 + 资源 `?v` 版本 + 当日额度行样式/脚本 + `/api/submit` 卡片额度字段
（黄/红卡携带、绿卡不携带）。仅标准库，退出码 0 = 全过。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import uuid


def _get(base: str, path: str) -> str:
    with urllib.request.urlopen(base + path, timeout=15) as response:
        return response.read().decode("utf-8")


def _post(base: str, path: str, body: dict[str, object]) -> dict[str, object]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def run(base: str) -> list[str]:
    failures: list[str] = []
    session_id = f"page-check-{uuid.uuid4().hex}"

    html = _get(base, "/")
    css = _get(base, "/assets/app.css?v=7")
    workspace_css = _get(base, "/assets/workspace.css?v=53")
    js = _get(base, "/assets/app.js?v=82")

    checks = {
        "page: 计划卡容器": 'id="stage"' in html and "计划卡" in html,
        "page: 顶栏/三栏/处理详情": (
            'class="topbar"' in html and 'class="layout layout--workspace"' in html
            and 'class="side"' in html and 'class="main"' in html and 'class="trace rail"' in html
        ),
        "page: 视图层（首页/对话/记录/我的）": (
            'id="view-home"' in html and 'id="view-chat"' in html
            and 'id="view-records"' in html and 'id="view-health"' not in html
            and 'id="view-me"' in html and 'id="profile-panel"' in html
        ),
        "page: 窄屏首页列表容器": 'id="home-scene-list"' in html,
        "page: 左栏无「常用办理」": "常用办理" not in html and 'id="quick-scenes"' not in html,
        "page: 右栏无三色分级图例": "三色分级（官方口径）" not in html,
        "page: 对话式结构": (
            'id="conversation-sidebar"' in html
            and 'id="conversation-welcome"' in html
            and 'id="task-guard"' in html
            and 'class="composer"' in html
        ),
        "page: 抽屉 aria 目标": (
            'id="conversation-sidebar"' in html
            and 'id="trace"' in html
            and 'aria-controls="trace"' in html
        ),
        "page: 资源 ?v": (
            "/assets/app.css?v=7" in html
            and "/assets/workspace.css?v=53" in html
            and "/assets/app.js?v=82" in html
        ),
        "page: PWA 安装与订阅入口": (
            'id="btn-install"' in html
            and 'id="install-status"' in html
            and 'data-record-tab="charges"' in html
            and 'data-action="refresh-subscriptions"' in html
        ),
        "page: 场景①额度卡容器": 'id="quota-panel"' in html and 'id="quota-body"' in html,
        "page: 不露原始骑缝哈希": "骑缝哈希（cfhash）" not in html,
        "css: .quota 额度行样式": ".quota {" in css and ".quota__val" in css,
        "css: 超额强调": ".quota--over" in css,
        "css: .tech 默认收起": ".tech {" in css and "display: none;" in css,
        "workspace: 三栏/抽屉样式": (
            ".layout.layout--workspace" in workspace_css
            and ".shell.is-trace-open .trace" in workspace_css
        ),
        "js: 额度行渲染": (
            "function quotaHtml(card)" in js and "本人当日限额" in js and "已超额" in js
        ),
        "js: 读额度字段": "daily_limit_cents" in js and "daily_accumulated_cents" in js,
        "js: 链节点中文+tech": "function chainAdd(label, tech)" in js and "node.label" in js,
        "js: 中文级别名": 'name: "黄色"' in js,
        "js: 窄屏我的为页面": 'isAppShell()' in js and 'navigate("me"' in js,
        "js: 规则字典（真实枚举）": "AMOUNT_OVER_DAILY_LIMIT:" in js and "QUERY_ONLY:" in js,
        "js: 要求字典": "user_confirm: " in js and "strong_verify: " in js,
        "js: 凭证号替代哈希": "function receiptNo(" in js,
        "js: 红色强验证挑战": (
            "strong-verify" in js and "获取验证码" in js and "/api/verify/request" in js
        ),
        "js: 执行前预演": "function previewHtml(" in js and "确认后会发生什么" in js,
        "js: 数值溯源": "provenance-panel" in js and "/api/audit?limit=" in js,
        "js: AI 三角色链路": (
            "function roleChainHtml(" in js and "规则风险官" in js and "规则引擎，非 LLM" in js
        ),
        "workspace: 三角色卡样式": (
            ".role-chain {" in workspace_css and ".role-card--red" in workspace_css
        ),
        "js: 订阅价格与下次扣款": (
            "item.price_increased_12m === true" in js
            and "item.price_increased_12m === false" in js
            and "样本不足" in js
            and "下次扣款日" in js
        ),
        "js: 扣款记录复用通用表格": (
            'if (key === "charges")' in js
            and 'class="charge-records-table"' not in js
            and "seen.has(key0)" in js
            and "data.subscription_details" in js
        ),
        "js: 审计渠道与摘要": (
            'payload.channel || "未记录"' in js
            and "payload.text_digest" in js
            and "同摘要关联" in js
        ),
        "js: PWA 安装交互": (
            "beforeinstallprompt" in js
            and "appinstalled" in js
            and 'register("/assets/sw.js?v=72"' in js
        ),
        "js: 服务端时间按 UTC 解析": (
            "function parseServerTime(stamp)" in js
            and "parseServerTime(state.card.expires_at)" in js
        ),
    }
    bad = [key for key, ok in checks.items() if not ok]
    for key, ok in checks.items():
        print(("OK   " if ok else "MISS ") + key)
    failures.extend(bad)

    selected = {"account": "acc_savings", "card": "card_debit", "subscription": "sub_video"}

    yellow = _post(base, "/api/submit", {
        "session_id": session_id, "utterance": "转给李娜 500 元", "selected": selected,
    })
    ycard = yellow.get("card") or {}
    limit = ycard.get("daily_limit_cents")
    used = ycard.get("daily_accumulated_cents")
    ylevel = ycard.get("level")
    # 额度字段与级别须一致：黄 ⇔ 未超（used ≤ limit）、红 ⇔ 超（used > limit）。
    # 不断言恒为黄：演示库日累计可能已抬升级别（与 e2e 自检同口径）。
    y_ok = (
        isinstance(limit, int)
        and isinstance(used, int)
        and ylevel in {"YELLOW", "RED"}
        and ((ylevel == "YELLOW") == (used <= limit))
        and ((ylevel == "RED") == (used > limit))
    )
    print(f"[api] 转账500额度一致 -> level={ylevel} limit={limit} used={used} 通过={y_ok}")
    if not y_ok:
        failures.append("黄/红卡额度字段与级别不一致")

    red = _post(base, "/api/submit", {
        "session_id": session_id, "utterance": "转给李娜 1500 元", "selected": selected,
    })
    rcard = red.get("card") or {}
    rlimit = rcard.get("daily_limit_cents")
    rused = rcard.get("daily_accumulated_cents")
    r_ok = (
        rcard.get("level") == "RED"
        and isinstance(rlimit, int)
        and isinstance(rused, int)
        and rused > rlimit
    )
    print(f"[api] 红卡超额 -> level={rcard.get('level')} limit={rlimit} used={rused} 通过={r_ok}")
    if not r_ok:
        failures.append("红卡未体现超额（daily_accumulated_cents 应 > daily_limit_cents）")

    green = _post(base, "/api/submit", {
        "session_id": session_id, "utterance": "查一下上月账单", "selected": selected,
    })
    gcard = green.get("card") or {}
    g_ok = (
        gcard.get("level") == "GREEN"
        and "daily_limit_cents" not in gcard
        and "daily_accumulated_cents" not in gcard
    )
    print(f"[api] 绿卡无额度 -> level={gcard.get('level')} keys={sorted(gcard.keys())} 通过={g_ok}")
    if not g_ok:
        failures.append("绿卡不应携带额度字段")

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="服务页自检（对真实服务发 HTTP 请求）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args(argv)
    base = str(args.base_url).rstrip("/")
    print(f"target: {base}\n")
    failures = run(base)
    print()
    if failures:
        print("FAILED:")
        for item in failures:
            print("  -", item)
        return 1
    print("ALL OK：内联页 + 资源版本 + 额度行样式/脚本 + 卡片额度字段（黄/红携带、绿卡不携带）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
