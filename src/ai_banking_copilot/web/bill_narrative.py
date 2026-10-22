"""账单结论式分析的模型通道：只解释已核实的 `Finding`，不产事实、不算金额。

链路位置：`scenarios.bill_findings.analyze_bill`（确定性识别）→ 本模块（模型解释）
→ `scenarios.bill_findings.validate_narrative`（结构 + 证据校验）→ 失败则确定性降级。

约束：
- 输入只含已回读的交易明细与 Finding 证据，不含原始商户备注；
- 输出必须逐条对应既有 `finding_id`，不得新增交易、金额或确认状态；
- 本模块**不修改**报告里的任何金额，模型只生成文字。
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
from ai_banking_copilot.scenarios.bill_findings import BillFindingsReport

NARRATIVE_PROMPT_VERSION = "bill-narrative-v1"
NARRATIVE_TASK_ID = "bill-narrative"

_SYSTEM_PROMPT = (
    "你是银行账单解读助手。输入是已经由确定性规则核实的账单明细与识别结果，"
    "你只负责解释原因、归纳问题、说明影响并给出可执行建议。\n"
    "硬性规则：\n"
    "1. 只能解释输入中已有的 finding_id，每个 finding_id 恰好输出一条，不得新增或遗漏。\n"
    "2. 不得新增交易、交易编号、金额、比例或任何数字；金额与结论一律以输入为准，"
    "需要提及时用「金额」「差额」等词指代，不得写出具体数值或货币单位。\n"
    "3. status 为 suspected 的项必须表述为「需要核对 / 可能」，不得写成已经确认的问题；"
    "status 为 normal 的项只能说明其属于已知正常扣费。\n"
    "4. 不得声称已经退款、追回、取消、解决或确认扣费错误；不得给出成功率、节省比例等量化承诺。\n"
    "5. 只输出 JSON，不输出解释性前缀或代码块。\n"
    '输出格式：{"summary":"整体结论","findings":[{"finding_id":"...",'
    '"explanation":"原因与影响","suggestion":"具体可执行建议"}]}。'
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
    """整理后的输入：账期、数据来源、明细摘要与 Finding 证据（不含原始备注）。"""
    return json.dumps(
        {
            "period": report.period,
            "as_of": report.as_of.isoformat(),
            "data_source": report.data_source,
            "data_source_is_demo": True,
            "confirmed_saving_cents": report.confirmed_saving_cents,
            "potential_saving_cents": report.potential_saving_cents,
            "notes": list(report.notes),
            "transactions": [
                {
                    "txn_id": item.txn_id,
                    "booked_on": item.booked_on.isoformat(),
                    "amount_cents": item.amount_cents,
                    "category": item.category,
                    "merchant_label": item.merchant_label,
                }
                for item in report.transactions
            ],
            "findings": [
                {
                    "finding_id": finding.finding_id,
                    "kind": finding.kind,
                    "status": finding.status,
                    "confidence": str(finding.confidence),
                    "txn_ids": list(finding.txn_ids),
                    "impact_cents": finding.impact_cents,
                    "impact_basis": finding.impact_basis,
                    "saving_basis": finding.saving_basis,
                    "assumption": finding.assumption,
                    "detail": finding.detail,
                    "evidence": [
                        {
                            "txn_id": item.txn_id,
                            "record_id": item.record_id,
                            "booked_on": item.booked_on.isoformat(),
                            "amount_cents": item.amount_cents,
                            "merchant_label": item.merchant_label,
                        }
                        for item in finding.evidence
                    ],
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
