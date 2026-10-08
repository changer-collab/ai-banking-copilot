"""设计仓与主仓前端资产的**漂移检测**（P2-13）。

背景：主仓 `src/ai_banking_copilot/web/assets/` 与设计仓 `assets/` 存在重复文件
（`tokens.css` / `app.css` 逐字相同），靠人工约定同步。本脚本把「是否已漂移」变成可执行检查，
但**不引入同步机制**（submodule / 构建期生成 / CI 强制同步由团队另行裁决）。

判据：
- **FAIL**：设计令牌与组件层样式（`tokens.css` / `app.css`）两仓不一致——这两份是设计规范本体，
  不一致说明「设计稿与实现版」已经分叉；
- **WARN**：术语字典（`GLOSS.rule`）键集不一致——设计仓是 mock，含示意性规则码可能是**有意**的，
  故只提示不判失败。

设计仓不在本地时**跳过**（exit 0），避免在只检出主仓的 CI 上误报。

用法：
    python scripts/check_design_sync.py
    python scripts/check_design_sync.py --design-repo ../ai-banking-copilot-web
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PRIMARY_ASSETS = REPO_ROOT / "src" / "ai_banking_copilot" / "web" / "assets"
DEFAULT_DESIGN_REPO = REPO_ROOT.parent / "ai-banking-copilot-web"

SYNCED_FILES = ("tokens.css", "app.css")
"""必须逐字一致的文件：设计令牌 + 组件层样式。"""

_GLOSS_RULE = re.compile(r"rule:\s*\{(?P<body>[^}]*)\}", re.DOTALL)
_RULE_KEY = re.compile(r"([A-Z][A-Za-z0-9_]*)\s*:")


def _gloss_rule_keys(path: Path) -> set[str]:
    """抽取 `GLOSS.rule` 的规则码键集（找不到即空集，不抛异常）。"""
    if not path.is_file():
        return set()
    match = _GLOSS_RULE.search(path.read_text(encoding="utf-8"))
    return set(_RULE_KEY.findall(match.group("body"))) if match else set()


def main() -> int:
    parser = argparse.ArgumentParser(description="检测设计仓与主仓前端资产是否漂移")
    parser.add_argument(
        "--design-repo",
        type=Path,
        default=DEFAULT_DESIGN_REPO,
        help="设计仓根目录（默认取主仓同级目录）",
    )
    args = parser.parse_args()
    design_repo: Path = args.design_repo

    if not (design_repo / "assets").is_dir():
        print(f"SKIPPED：未找到设计仓 assets/（{design_repo}）——本检查需要两仓同时在本地")
        return 0

    print(f"主仓：{PRIMARY_ASSETS}")
    print(f"设计仓：{design_repo / 'assets'}\n")

    failures: list[str] = []
    for name in SYNCED_FILES:
        primary = PRIMARY_ASSETS / name
        design = design_repo / "assets" / name
        if not primary.is_file() or not design.is_file():
            in_primary, in_design = primary.is_file(), design.is_file()
            failures.append(f"{name}：一侧缺失（主仓 {in_primary}｜设计仓 {in_design}）")
            print(f"MISS {name}（一侧缺失）")
            continue
        if primary.read_bytes() == design.read_bytes():
            print(f"OK   {name}（逐字一致）")
        else:
            failures.append(f"{name}：两仓内容不一致（已漂移）")
            print(f"MISS {name}（已漂移）")

    primary_rules = _gloss_rule_keys(PRIMARY_ASSETS / "app.js")
    design_rules = _gloss_rule_keys(design_repo / "assets" / "demo.js")
    only_design = sorted(design_rules - primary_rules)
    only_primary = sorted(primary_rules - design_rules)
    if only_design or only_primary:
        print("\nWARN 术语字典 GLOSS.rule 键集不一致（设计仓为 mock，可能属有意）：")
        if only_design:
            print(f"  仅设计仓有：{', '.join(only_design)}")
        if only_primary:
            print(f"  仅主仓有：{', '.join(only_primary)}")
    else:
        print("\nOK  术语字典 GLOSS.rule 键集一致")

    if failures:
        print("\nFAILED：")
        for item in failures:
            print(f"  - {item}")
        print(
            "\n处置：主仓 `web/assets/` 为实现版、设计仓为规范版，"
            "以设计仓为准同步（见 CONTRIBUTING.md），或另行裁决 submodule / 构建期生成。"
        )
        return 1

    print("\nALL OK：设计令牌与组件层样式两仓一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
