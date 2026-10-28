"""风险测评问卷契约：题库（确定性）→ 得分 → R 档。

分层纪律（事实零生成）：
- **等级只能由本模块的确定性评分算出**：请求体只提交「题号 → 选项号」，不提交等级；
- 题库、选项、分值全部写死在代码里，不经过模型，也不接受客户端上传分值；
- 未答满 / 题号未知 / 选项未知一律抛 `AssessmentError`（安全拒绝，不猜、不补默认值）。

前端只需消费 `questionnaire_payload()` 的数据结构，并把用户选择原样回传。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

_RISK_PROFILES = ("R1", "R2", "R3", "R4", "R5")


class AssessmentError(ValueError):
    """问卷答案不合法（缺题 / 未知题号 / 未知选项）：拒绝评分。"""


@dataclass(frozen=True)
class AssessmentOption:
    """单个选项：`id` 是回传值，`score` 是服务端私有分值（不下发前端）。"""

    id: str
    label: str
    score: int


@dataclass(frozen=True)
class AssessmentQuestion:
    """单道题：题号、题干、固定顺序的选项。"""

    id: str
    prompt: str
    options: tuple[AssessmentOption, ...]

    def option(self, option_id: str) -> AssessmentOption:
        """按选项号取选项；未知选项抛 `AssessmentError`。"""
        for candidate in self.options:
            if candidate.id == option_id:
                return candidate
        raise AssessmentError(f"题目 {self.id} 没有选项 {option_id}")


@dataclass(frozen=True)
class AssessmentResult:
    """一次问卷的结构化结果（等级来自 `score_to_profile`，不接受外部指定）。"""

    risk_profile: str
    score: int
    max_score: int
    answers: tuple[tuple[str, str], ...]
    """`(题号, 选项号)`，顺序与题库一致，便于回放与审计。"""


QUESTIONNAIRE: tuple[AssessmentQuestion, ...] = (
    AssessmentQuestion(
        id="q1_surplus",
        prompt="每个月发完工资、交完房租和日常开销，您大概还能剩下多少？",
        options=(
            AssessmentOption("q1_a", "基本剩不下，有时还要动用存款", 0),
            AssessmentOption("q1_b", "能剩下一点，不到收入的三成", 1),
            AssessmentOption("q1_c", "能剩下收入的三成到一半", 2),
            AssessmentOption("q1_d", "能剩下收入的一半以上", 3),
        ),
    ),
    AssessmentQuestion(
        id="q2_experience",
        prompt="您买过银行理财或者基金吗？大概买了多久？",
        options=(
            AssessmentOption("q2_a", "没买过，不太了解", 0),
            AssessmentOption("q2_b", "买过，不到 1 年", 1),
            AssessmentOption("q2_c", "买了 1–3 年", 2),
            AssessmentOption("q2_d", "买了 3 年以上", 3),
        ),
    ),
    AssessmentQuestion(
        id="q3_drawdown",
        prompt="假设您买的理财暂时亏了，最多亏多少您还能接受？",
        options=(
            AssessmentOption("q3_a", "一点亏损都不能接受", 0),
            AssessmentOption("q3_b", "亏 5% 以内", 1),
            AssessmentOption("q3_c", "亏 5%–15%", 2),
            AssessmentOption("q3_d", "亏 15% 以上也能接受", 3),
        ),
    ),
    AssessmentQuestion(
        id="q4_horizon",
        prompt="这笔钱大概多久之内您不会用到？",
        options=(
            AssessmentOption("q4_a", "3 个月内就要用", 0),
            AssessmentOption("q4_b", "3 个月到 1 年", 1),
            AssessmentOption("q4_c", "1–3 年", 2),
            AssessmentOption("q4_d", "3 年以上都用不到", 3),
        ),
    ),
    AssessmentQuestion(
        id="q5_reaction",
        prompt="刚买没多久就跌了 10%，您通常会怎么做？",
        options=(
            AssessmentOption("q5_a", "马上全部卖出", 0),
            AssessmentOption("q5_b", "先卖出一部分", 1),
            AssessmentOption("q5_c", "先放着，继续观察", 2),
            AssessmentOption("q5_d", "趁跌了再买一些", 3),
        ),
    ),
)
"""五道固定题：每题 4 个选项、分值 0–3，总分 0–15。"""

MAX_SCORE = sum(max(option.score for option in question.options) for question in QUESTIONNAIRE)
"""问卷满分（由题库推导，改题自动跟随）。"""

ASSESSMENT_REQUEST_HINTS = (
    "风险测评",
    "风险评测",
    "重新测评",
    "做测评",
    "做一次测评",
    "测一下风险",
)
"""「我要做风险测评」类请求的确定性判据：命中即出问卷，**绝不反问用户等级**。"""


def is_assessment_request(text: str) -> bool:
    """文本是否在要求做风险测评（字符串判据，不经过模型）。"""
    return any(hint in text for hint in ASSESSMENT_REQUEST_HINTS)


def invitation_message() -> str:
    """出题话术：把 5 道题原文带出来，用户不必再去找入口。"""
    lines = ["风险测评一共 5 道题，答完由系统算分定级（R1–R5），等级不能自填。"]
    lines.extend(
        f"{index}. {question.prompt}"
        for index, question in enumerate(QUESTIONNAIRE, start=1)
    )
    lines.append("在下方逐题选好提交即可（右侧「理财 · 测评与持仓」面板里也能答）。")
    return "\n".join(lines)


def questionnaire_payload() -> tuple[dict[str, object], ...]:
    """前端契约：题号 / 题干 / 选项号 / 选项文案；**不下发分值**。"""
    return tuple(
        {
            "id": question.id,
            "prompt": question.prompt,
            "options": [{"id": option.id, "label": option.label} for option in question.options],
        }
        for question in QUESTIONNAIRE
    )


def score_to_profile(score: int, max_score: int = MAX_SCORE) -> str:
    """按得分占比映射 R 档（`R1` 最保守 → `R5` 最激进）。"""
    if max_score <= 0:
        raise AssessmentError("问卷满分必须为正数")
    if isinstance(score, bool) or not isinstance(score, int):
        raise AssessmentError("得分必须是整数")
    if not 0 <= score <= max_score:
        raise AssessmentError(f"得分 {score} 超出 0–{max_score}")
    ratio = score / max_score
    if ratio < 0.2:
        return _RISK_PROFILES[0]
    if ratio < 0.4:
        return _RISK_PROFILES[1]
    if ratio < 0.6:
        return _RISK_PROFILES[2]
    if ratio < 0.8:
        return _RISK_PROFILES[3]
    return _RISK_PROFILES[4]


def evaluate_answers(answers: Mapping[str, str]) -> AssessmentResult:
    """把 `题号 → 选项号` 评成分数与本模块算出的 R 档。

    **必须答满**：缺任何一题都不评（不补默认值、不按已答题目折算）；
    未知题号 / 未知选项同样拒绝（输入零信任）。
    """
    expected = [question.id for question in QUESTIONNAIRE]
    unknown = sorted(set(answers) - set(expected))
    if unknown:
        raise AssessmentError(f"存在未知题号：{'、'.join(unknown)}")
    missing = [question_id for question_id in expected if question_id not in answers]
    if missing:
        raise AssessmentError(f"还有题目未作答：{'、'.join(missing)}")

    score = 0
    ordered: list[tuple[str, str]] = []
    for question in QUESTIONNAIRE:
        option_id = answers[question.id]
        if not isinstance(option_id, str) or not option_id:
            raise AssessmentError(f"题目 {question.id} 的选项必须是非空文本")
        score += question.option(option_id).score
        ordered.append((question.id, option_id))
    return AssessmentResult(
        risk_profile=score_to_profile(score),
        score=score,
        max_score=MAX_SCORE,
        answers=tuple(ordered),
    )
