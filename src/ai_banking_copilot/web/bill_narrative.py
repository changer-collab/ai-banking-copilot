"""账单结论式分析的模型通道：只解释已核实的 `Finding`，不产事实、不算金额。

链路位置：`scenarios.bill_findings.analyze_bill`（确定性识别）→ 本模块（选择受限选项）
→ `scenarios.bill_findings.validate_narrative`（ID + 事实匹配校验并服务端渲染）→ 失败则确定性降级。

约束：
- 发给模型的输入只含已登记选择 ID，不含账户、金额、实体或交易结果事实；
- 输出必须逐条对应既有 `finding_id`，并且只选择服务端登记的解释/建议 ID；
- 本模块**不修改**报告里的任何金额，金额、实体和结果均由服务端渲染。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from ai_banking_copilot.planning.call_observation import CallObserver
from ai_banking_copilot.planning.llm import (
    DEFAULT_LLM_ENDPOINT,
    DEFAULT_LLM_MODEL,
    HttpTransport,
    LlmUnavailableError,
    _load_httpx_transport,
)
from ai_banking_copilot.planning.model_config import ModelConfig, NonBlockingLimiter
from ai_banking_copilot.scenarios.bill_findings import (
    NARRATIVE_SUMMARY_ID,
    BillFindingsReport,
    narrative_choice_ids,
)

NARRATIVE_PROMPT_VERSION = "bill-narrative-v2"
NARRATIVE_TASK_ID = "bill-narrative"

_SYSTEM_PROMPT = (
    "你是银行账单解读助手。输入包含确定性识别结果对应的结构化选择列表，"
    "你只负责从输入给出的登记选项中选择解释与建议。\n"
    "硬性规则：\n"
    "1. 只能解释输入中已有的 finding_id，每个 finding_id 恰好输出一条，不得新增或遗漏。\n"
    "2. 只能输出输入中列出的 summary_id、explanation_id、suggestion_id，禁止输出自由文本。\n"
    "3. 不得新增交易、交易编号、金额、实体、比例、结果或任何数字；这些内容由服务端渲染。\n"
    "4. 不得声称已经退款、追回、取消、解决或确认扣费错误。\n"
    "5. 只输出 JSON，不输出解释性前缀或代码块。\n"
    '输出格式：{"summary_id":"findings_overview","findings":[{"finding_id":"...",'
    '"explanation_id":"...","suggestion_id":"..."}]}。'
)


class LlmBillNarrator:
    """真实 LLM 叙事通道：可注入传输、懒加载、任何失败都上抛 `LlmUnavailableError`。"""

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        api_key: str | None = None,
        model: str = DEFAULT_LLM_MODEL,
        endpoint: str = DEFAULT_LLM_ENDPOINT,
        timeout_s: float = 20.0,
        observer: CallObserver | None = None,
        config: ModelConfig | None = None,
    ) -> None:
        settings = config
        self._transport = transport
        self._api_key = (
            api_key
            if api_key is not None
            else settings.api_key
            if settings is not None
            else os.environ.get("DASHSCOPE_API_KEY")
        )
        self._model = (
            settings.model if settings is not None and model == DEFAULT_LLM_MODEL else model
        )
        self._endpoint = (
            settings.endpoint
            if settings is not None and endpoint == DEFAULT_LLM_ENDPOINT
            else endpoint
        )
        self._timeout_s = (
            settings.planner_timeout_s if settings is not None and timeout_s == 20.0 else timeout_s
        )
        self._observer = observer
        self._limiter = settings.limiter if settings is not None else NonBlockingLimiter(1)

    @property
    def loaded(self) -> bool:
        return self._transport is not None

    def narrate(self, report: BillFindingsReport) -> Mapping[str, object]:
        """构造受约束的请求并解析模型 JSON；不可用或不可解析时抛 `LlmUnavailableError`。"""
        transport = self._ensure_transport()
        request = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _user_payload(report)},
                ],
            },
            ensure_ascii=False,
        )
        try:
            raw = transport(request)
        except LlmUnavailableError:
            raise
        except Exception as error:
            raise LlmUnavailableError("账单解释调用失败（transport_error）") from error
        return _parse_narrative(raw)

    def _ensure_transport(self) -> HttpTransport:
        if self._transport is not None:
            return self._transport
        api_key = self._api_key
        if not api_key:
            raise LlmUnavailableError("缺少 LLM 凭据（环境变量 DASHSCOPE_API_KEY）")
        transport = _load_httpx_transport(
            api_key=api_key,
            endpoint=self._endpoint,
            timeout_s=self._timeout_s,
            observer=self._observer,
            limiter=self._limiter,
            task_id=NARRATIVE_TASK_ID,
            prompt_version=NARRATIVE_PROMPT_VERSION,
            role="bill-narrative",
            channel="llm-narrative",
        )
        self._transport = transport
        return transport


def _user_payload(report: BillFindingsReport) -> str:
    """只发送标识符与允许选项；模型不接收事实数据。"""
    return json.dumps(
        {
            "summary_id": NARRATIVE_SUMMARY_ID,
            "findings": [
                {
                    "finding_id": finding.finding_id,
                    "narrative_options": narrative_choice_ids(finding),
                }
                for finding in report.findings
            ],
        },
        ensure_ascii=False,
    )


def _parse_narrative(raw: str) -> Mapping[str, object]:
    """解析模型文本为 JSON 对象；格式不符统一归一为 `LlmUnavailableError`。"""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LlmUnavailableError("账单解释响应不是合法 JSON（format_error）") from error
    if not isinstance(payload, dict):
        raise LlmUnavailableError("账单解释响应必须是 JSON 对象")
    return payload
