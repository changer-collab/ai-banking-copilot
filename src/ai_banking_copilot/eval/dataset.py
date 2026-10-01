"""评测用例契约与加载器：把标准指令 / 对抗样例 / held-out 变成可机读的 `EvalCase`。

设计要点：
- **冻结五字段**：`id` / `utterance` / `expect_level` / `expect_blocked` / `category`——
  与 `README.md` 冻结面一致，不得扩展或改名。
- **自洽性构造即校验**：`expect_blocked=True` 时必须 `expect_level == "NONE"`
  （澄清 / 安全拒绝**不产出级别**，PRD §5.1 / §5.7）；反之亦然。
- **held-out 独立加载**：`load_heldout_cases` 与迭代用例分离，保证 held-out 不参与迭代。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

LEVEL_NAMES: tuple[str, ...] = ("GREEN", "YELLOW", "RED", "MANUAL", "DENY")
"""合法授权级别名（与 `contracts.authorization.Level` 的成员名逐项一致）。"""

NO_LEVEL: str = "NONE"
"""不产出级别的哨兵：澄清 / 安全拒绝 / 命中吸收态否决的用例用它占位。"""

EVAL_DATA_DIR: Path = Path(__file__).resolve().parent / "data"
"""迭代用例所在目录（标准指令 + 对抗样例）。"""

CASE_FILES: tuple[str, ...] = ("cases.json", "adversarial.json")
"""迭代用用例文件（顺序即加载顺序）。"""

HELD_OUT_FILE = "heldout.json"
"""held-out 文件（实现冻结后另建，不参与迭代）。"""

MUST_PASS_IDS: tuple[str, ...] = (
    "transfer-yellow",
    "bills-green",
    "cards-loss-red",
    "transfer-blocked-payee",
    "adv-injection-1",
    "transfer-multi-candidate",
)
"""must-pass 外部锚：这些用例**必须全部通过**（PRD §9.2）。"""

_LEVEL_ORDER: dict[str, int] = {name: index for index, name in enumerate(LEVEL_NAMES)}


@dataclass(frozen=True)
class EvalCase:
    """一条评测用例：一句自然语言指令 + 期望的授权结论。"""

    id: str
    utterance: str
    expect_level: str
    expect_blocked: bool
    category: str

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("EvalCase.id 不能为空")
        if not self.utterance:
            raise ValueError(f"{self.id} 的 utterance 不能为空")
        if not self.category:
            raise ValueError(f"{self.id} 的 category 不能为空")
        if self.expect_level not in (*LEVEL_NAMES, NO_LEVEL):
            raise ValueError(f"{self.id} 的 expect_level 非法：{self.expect_level}")
        if self.expect_blocked and self.expect_level != NO_LEVEL:
            raise ValueError(f"{self.id}：expect_blocked=True 时 expect_level 必须是 {NO_LEVEL}")
        if not self.expect_blocked and self.expect_level == NO_LEVEL:
            raise ValueError(f"{self.id}：未期望拦截时 expect_level 不得为 {NO_LEVEL}")

    @property
    def expected_order(self) -> int | None:
        """期望级别的序关系值（`NONE` 表示不产出级别）。"""
        if self.expect_level == NO_LEVEL:
            return None
        return _LEVEL_ORDER[self.expect_level]


def load_cases(path: str | Path) -> tuple[EvalCase, ...]:
    """从 JSON 文件读取用例；文件必须是对象数组，且 `id` 不重复。"""
    text = Path(path).read_text(encoding="utf-8")
    raw: object = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError(f"用例文件必须是 JSON 数组：{path}")
    cases = tuple(_case_of(item, path) for item in raw)
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError(f"用例 id 重复：{path}")
    return cases


def load_default_cases(data_dir: str | Path | None = None) -> tuple[EvalCase, ...]:
    """加载迭代用用例（标准指令 + 对抗样例），**不含 held-out**。"""
    directory = Path(data_dir) if data_dir is not None else EVAL_DATA_DIR
    cases: list[EvalCase] = []
    for name in CASE_FILES:
        cases.extend(load_cases(directory / name))
    return tuple(cases)


def load_heldout_cases(data_dir: str | Path | None = None) -> tuple[EvalCase, ...]:
    """加载 held-out 用例（实现冻结后另建，不参与开发迭代）。"""
    directory = Path(data_dir) if data_dir is not None else EVAL_DATA_DIR
    return load_cases(directory / HELD_OUT_FILE)


def _case_of(item: object, path: object) -> EvalCase:
    if not isinstance(item, dict):
        raise ValueError(f"用例必须是 JSON 对象：{path}")
    return EvalCase(
        id=str(item["id"]),
        utterance=str(item["utterance"]),
        expect_level=str(item["expect_level"]),
        expect_blocked=bool(item["expect_blocked"]),
        category=str(item["category"]),
    )
