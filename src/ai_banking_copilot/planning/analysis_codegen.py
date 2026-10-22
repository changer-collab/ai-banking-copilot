"""账单分析 DSL 生成与父进程校验。

模型只看分析任务和快照字段名；事实记录在事实网关取出后才交给沙箱。父进程先
用同一组结构约束校验模型文本，沙箱子进程随后再次解析和校验表达式。
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from enum import StrEnum
from typing import Final

from ai_banking_copilot.planning.model_config import ModelConfig


class AnalysisTask(StrEnum):
    SUM = "sum"
    YOY = "yoy"


class AnalysisCodeGenerationError(ValueError):
    """模型输出不是该分析任务允许的受限 DSL。"""


AnalysisCodeTransport = Callable[[str], str]


_TASK_FIELDS: Final[dict[AnalysisTask, int]] = {
    AnalysisTask.SUM: 1,
    AnalysisTask.YOY: 2,
}
_MAX_CODE_BYTES: Final[int] = 8_192
_MAX_AST_NODES: Final[int] = 32


def generate_analysis_code(
    task: AnalysisTask,
    field_names: tuple[str, ...],
    *,
    config: ModelConfig | None = None,
    transport: AnalysisCodeTransport | None = None,
) -> str:
    """生成并校验分析表达式。

    未启用真实模型（禁用开关、缺少凭据）时使用确定性模板。注入的 ``transport``
    只接收不含事实值的请求 JSON 字符串，便于离线测试。
    """
    settings = config if config is not None else ModelConfig.from_env()
    normalized = _normalize_fields(field_names)
    if settings.disable_llm or not settings.api_key:
        return validate_analysis_code(_template(task, normalized), task, normalized)

    sender = transport
    if sender is None:
        from ai_banking_copilot.planning.llm import (
            LlmUnavailableError,
            _load_httpx_transport,
        )

        try:
            sender = _load_httpx_transport(
                api_key=settings.api_key,
                endpoint=settings.endpoint,
                timeout_s=settings.planner_timeout_s,
                limiter=settings.limiter,
                role="analysis_codegen",
                channel="llm-strict",
            )
        except LlmUnavailableError as error:
            raise AnalysisCodeGenerationError("分析代码模型不可用") from error
    request = _request(task, normalized, settings.planner_model)
    try:
        raw = sender(request)
    except Exception as error:
        raise AnalysisCodeGenerationError("分析代码模型调用失败") from error
    code = _extract_code(raw)
    return validate_analysis_code(code, task, normalized)


def validate_analysis_code(
    code: str, task: AnalysisTask, field_names: tuple[str, ...]
) -> str:
    """在父进程内拒绝常量、额外字段、额外算子和任意 Python 语法。"""
    normalized = _normalize_fields(field_names)
    text = code.strip()
    if not text:
        raise AnalysisCodeGenerationError("分析代码为空")
    if len(text) > _MAX_CODE_BYTES:
        raise AnalysisCodeGenerationError("分析代码超过父进程预算")
    try:
        if len(text.encode("utf-8")) > _MAX_CODE_BYTES:
            raise AnalysisCodeGenerationError("分析代码超过父进程预算")
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, RecursionError, UnicodeError) as error:
        raise AnalysisCodeGenerationError("分析代码语法非法") from error
    if len(tuple(ast.walk(tree))) > _MAX_AST_NODES:
        raise AnalysisCodeGenerationError("分析代码超过父进程 AST 预算")
    if not isinstance(tree.body, ast.Call):
        raise AnalysisCodeGenerationError("分析代码必须调用白名单算子")
    call = tree.body
    if not isinstance(call.func, ast.Name) or call.func.id != task.value:
        raise AnalysisCodeGenerationError("分析代码算子与任务不匹配")
    expected_count = _TASK_FIELDS.get(task)
    if expected_count is None or len(call.args) != expected_count or call.keywords:
        raise AnalysisCodeGenerationError("分析代码参数数量不匹配")
    if len(normalized) != len(call.args):
        raise AnalysisCodeGenerationError("分析代码字段数量不匹配")
    if task is AnalysisTask.SUM:
        _require_field(call.args[0], normalized[0])
    else:
        for argument, field_name in zip(call.args, normalized, strict=True):
            if (
                not isinstance(argument, ast.Call)
                or not isinstance(argument.func, ast.Name)
                or argument.func.id != "sum"
                or len(argument.args) != 1
                or argument.keywords
            ):
                raise AnalysisCodeGenerationError("同比参数必须先分别汇总原始事实")
            _require_field(argument.args[0], field_name)
    if any(
        isinstance(node, (ast.Attribute, ast.Subscript, ast.Constant, ast.BinOp, ast.Lambda))
        for node in ast.walk(tree)
    ):
        raise AnalysisCodeGenerationError("分析代码包含不允许的表达式")
    return text


def _normalize_fields(field_names: tuple[str, ...]) -> tuple[str, ...]:
    if not field_names or any(
        not isinstance(name, str) or not name.isidentifier() for name in field_names
    ):
        raise AnalysisCodeGenerationError("分析字段名非法")
    if len(set(field_names)) != len(field_names):
        raise AnalysisCodeGenerationError("分析字段名重复")
    return field_names


def _require_field(node: ast.expr, field_name: str) -> None:
    if not isinstance(node, ast.Name) or node.id != field_name:
        raise AnalysisCodeGenerationError("分析代码只能逐字引用指定字段")


def _template(task: AnalysisTask, fields: tuple[str, ...]) -> str:
    if task is AnalysisTask.SUM and len(fields) == 1:
        return f"sum({fields[0]})"
    if task is AnalysisTask.YOY and len(fields) == 2:
        return f"yoy(sum({fields[0]}), sum({fields[1]}))"
    raise AnalysisCodeGenerationError("分析任务字段不匹配")


def _request(task: AnalysisTask, fields: tuple[str, ...], model: str) -> str:
    return json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你只生成受限分析 DSL。只能使用 sum 或 yoy，不能使用常量、"
                        "属性、索引、导入、赋值或其他 Python。sum 任务只能聚合唯一字段；"
                        "yoy 任务必须写成 yoy(sum(第一字段), sum(第二字段))，"
                        "先分别汇总两组原始事实。只输出一行表达式。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"task": task.value, "fields": list(fields)},
                        ensure_ascii=False,
                    ),
                },
            ],
        },
        ensure_ascii=False,
    )


def _extract_code(raw: object) -> str:
    if not isinstance(raw, str):
        raise AnalysisCodeGenerationError("模型输出不是文本")
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(envelope, dict):
        choices = envelope.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str):
                    return content.strip()
    raise AnalysisCodeGenerationError("模型输出缺少分析代码")
