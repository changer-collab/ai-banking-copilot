"""账单分析场景：分类与统计**全走事实网关**；派生数字只走**白名单算子**并在沙箱执行。

口径（PRD §4.1 场景② / §5.1）：
- 数值只来自 `FactGateway.monthly_bill`（H1 事实零生成）；
- 派生数字（占比 / 同比 / 环比）只走 `gateway.operators` 白名单；
- 派生计算一律在 `gateway.sandbox` 的受限子进程中执行（无网络 / 禁库 / 禁写 / 限时）。
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import date
from decimal import Decimal

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    validate_plan,
)
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.errors import OperatorError, SandboxError
from ai_banking_copilot.gateway.facts import FactGateway, Record
from ai_banking_copilot.gateway.models import BillSummary, Money, Transaction
from ai_banking_copilot.gateway.operators import DerivedValue, run_operator
from ai_banking_copilot.gateway.sandbox import (
    SandboxMonitor,
    SandboxMonitorSink,
    SandboxRequest,
    SandboxResult,
    run_generated_analysis,
    run_in_sandbox,
)
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.analysis_codegen import (
    AnalysisCodeGenerationError,
    AnalysisTask,
    generate_analysis_code,
)
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.policy.pep import (
    QueryContext,
    QueryHandler,
    QueryResult,
    require_date,
    require_int,
    require_str,
)
from ai_banking_copilot.scenarios.base import (
    ClarificationRequired,
    ScenarioHandler,
    ScenarioRefused,
    as_int,
    as_str,
    grant_anchor,
    require_trusted,
    trusted_slot,
)
from ai_banking_copilot.scenarios.scheduling import REFERENCE_DATE

BILLS_ACTIONS = frozenset(
    {
        "account.balance",
        "account.transactions",
        "bills.monthly",
        "bills.category_trend",
        "bills.yearly",
    }
)
"""账单域动作（README 规范动作表 #1–#4 + 步骤 2 冻结的 `bills.yearly`）。"""

ANOMALY_THRESHOLD_CENTS: int = 500_000
"""异常大额阈值（**单位=分**，= 5000 元）。**展示层谓词**，不进算子 / PDP / PEP。"""

_CAPABILITY_BOUNDARIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("导出", "excel", "邮箱", "发我邮件"),
        "暂不支持导出 Excel 或发送邮件：当前只提供页面账单分析与记录页查询。",
    ),
    (
        ("深夜", "凌晨"),
        "暂不支持按时间点筛选交易；账单面板已把深夜交易标为可疑，请到「可疑清单」查看。",
    ),
    (
        ("在上海", "在深圳", "在北京", "在广州", "在杭州", "刷过吗", "哪个城市"),
        "暂不支持按城市筛选交易；账单面板已标记异地交易，请到「可疑清单」查看。",
    ),
    (
        ("退款", "退货", "到账了吗"),
        "暂不支持按退款状态查询到账；如需核对请提供具体交易日期与商户。",
    ),
)
"""能力边界（场景层）：不支持的问法一律明确说做不到，不拿大额扫描结果冒充答案。"""

_VAGUE_TRANSACTION = re.compile(
    r"(?:这|那)(?:一)?笔|是不是我刷的|是我刷的"
)
"""指代不明的「某笔消费核实」（如「这笔 6800 是不是我刷的」）。

这类问法没有可定位的键：流水里既没有用户口中的单据号，也没有「这笔」指向哪一行的确证。
**不允许**拿默认账户的当月大额扫描结果顶替答案（那会把"没找到大额"说成"不是你刷的"）。
"""

_VAGUE_TRANSACTION_PROMPT = (
    "无法定位你说的那一笔：交易流水里没有可反查的单据号，也没有「这笔」指向哪一行的依据。"
    "请补充交易日期与商户（例如「9 月 27 日凌晨那笔 6880」），"
    "或到「记录页 → 可疑清单」逐笔核对（那里带时间、城市、商户与置信度）。"
)
"""追问话术：给两个可执行出口（补日期商户 / 去可疑清单），不替用户猜是哪一笔。"""


def _vague_transaction(intents: Sequence[Intent]) -> bool:
    """判断这组意图里有没有「指代某笔」的原话片段（只看 span，不用它填任何控制槽）。"""
    for intent in intents:
        span = intent.slots.get("span")
        if isinstance(span, str) and _VAGUE_TRANSACTION.search(span):
            return True
    return False


def large_transactions(
    gateway: FactGateway, account_id: str, start: date, end: date
) -> tuple[Transaction, ...]:
    """只读：区间内「转出方向且金额 ≤ -阈值」的交易（单位=分、方向写死，禁与元比较）。"""
    return tuple(
        record.payload
        for record in gateway.transactions_between(account_id, start, end)
        if record.payload.direction == "out"
        and record.payload.amount.cents <= -ANOMALY_THRESHOLD_CENTS
    )


def describe_large_transactions(transactions: Sequence[Transaction]) -> str:
    """只拼事实（笔数 / 最大额 / 日期）；**绝不渲染 merchant 自由文本**（H3 展示安全）。"""
    if not transactions:
        return "未发现 ≥5000 元的大额支出"
    largest = max(transactions, key=lambda txn: (-txn.amount.cents, txn.txn_id))
    amount = Money(-largest.amount.cents).yuan
    return (
        f"其中 ≥5000 元支出 {len(transactions)} 笔，"
        f"最大 {amount} 元（{largest.booked_on.isoformat()}）"
    )


def category_totals(gateway: FactGateway, user_id: str, year: int, month: int) -> BillSummary:
    """分类统计**全走事实网关**（H1）。"""
    return gateway.monthly_bill(user_id, year, month).payload


def category_share(
    gateway: FactGateway, user_id: str, year: int, month: int, category: str
) -> DerivedValue:
    """分类占比：派生数字只走**白名单算子**（进程内，供沙箱结果交叉验证）。"""
    part, total, prefix = _share_inputs(gateway, user_id, year, month, category)
    return run_operator(
        "ratio",
        ((f"bills:{prefix}:{category}", Decimal(part)), (f"bills:{prefix}", Decimal(total))),
    )


def category_share_in_sandbox(
    gateway: FactGateway, user_id: str, year: int, month: int, category: str
) -> SandboxResult:
    """同一派生数字在**受限沙箱**中执行（无网络 / 禁库 / 禁写 / 限时）。"""
    part, total, prefix = _share_inputs(gateway, user_id, year, month, category)
    request = SandboxRequest(
        operator="ratio",
        inputs=((f"bills:{prefix}:{category}", Decimal(part)), (f"bills:{prefix}", Decimal(total))),
    )
    return run_in_sandbox(request)


class BillsHandler(ScenarioHandler):
    """账单分析场景处理器。"""

    @property
    def name(self) -> str:
        return "bills"

    def matches(self, intents: Sequence[Intent]) -> bool:
        return any(intent.action in BILLS_ACTIONS for intent in intents)

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        boundary = _capability_boundary(intents)
        if boundary is not None:
            raise ScenarioRefused(boundary)
        if _vague_transaction(intents):
            raise ClarificationRequired("transaction", _VAGUE_TRANSACTION_PROMPT)
        balances = [intent for intent in intents if intent.action == "account.balance"]
        account_txn = [
            intent for intent in intents if intent.action == "account.transactions"
        ]
        yearly = [intent for intent in intents if intent.action == "bills.yearly"]
        bills_specific = [
            intent
            for intent in intents
            if intent.action in {"bills.monthly", "bills.category_trend"}
        ]
        build_bills = bool(bills_specific)
        ledger = AnchorLedger()
        nodes: list[PlanNode] = []
        for index, intent in enumerate(balances, start=1):
            nodes.append(
                PlanNode(
                    node_id=f"balance{index}",
                    action="account.balance",
                    params={
                        "account_id": as_str(
                            trusted_slot(intent, ("account", "account_id"), "account"), "account"
                        ),
                    },
                    intent=IntentRef(
                        source=AnchorSource.EXPLICIT,
                        anchor_id=grant_anchor(ledger, AnchorSource.EXPLICIT, "账户余额查询"),
                    ),
                )
            )
        if account_txn:
            year, month = _period(intents)
            start, end = _month_range(year, month)
            account_ids: list[str] = []
            for intent in account_txn:
                if "account" in intent.slots or "account_id" in intent.slots:
                    account_ids.append(as_str(
                        trusted_slot(intent, ("account", "account_id"), "account"), "account"
                    ))
                else:
                    account_ids.extend(
                        record.payload.account_id for record in gateway.accounts_of(DEMO_USER_ID)
                    )
            for index, account_id in enumerate(account_ids, start=1):
                nodes.append(
                    PlanNode(
                        node_id=f"txn{index}",
                        action="account.transactions",
                        params={
                            "account_id": account_id,
                            "start": start.isoformat(),
                            "end": end.isoformat(),
                        },
                        intent=IntentRef(
                            source=AnchorSource.EXPLICIT,
                            anchor_id=grant_anchor(
                                ledger, AnchorSource.EXPLICIT, "异常交易查询"
                            ),
                        ),
                    )
                )
        if yearly:
            nodes.append(
                PlanNode(
                    node_id="y1",
                    action="bills.yearly",
                    params={"user_id": DEMO_USER_ID, "year": _year(intents)},
                    intent=IntentRef(
                        source=AnchorSource.EXPLICIT,
                        anchor_id=grant_anchor(ledger, AnchorSource.EXPLICIT, "年度账单汇总"),
                    ),
                )
            )
        elif build_bills:
            year, month = _period(intents)
            nodes.append(
                PlanNode(
                    node_id="n1",
                    action="bills.monthly",
                    params={"user_id": DEMO_USER_ID, "year": year, "month": month},
                    intent=IntentRef(
                        source=AnchorSource.EXPLICIT,
                        anchor_id=grant_anchor(ledger, AnchorSource.EXPLICIT, "账单月度统计"),
                    ),
                )
            )
            if any(intent.action == "bills.category_trend" for intent in bills_specific):
                summary = category_totals(gateway, DEMO_USER_ID, year, month)
                category = _chosen_category(intents, summary)
                base_year, base_month = _previous_month(year, month)
                nodes.append(
                    PlanNode(
                        node_id="n2",
                        action="bills.category_trend",
                        params={
                            "user_id": DEMO_USER_ID,
                            "category": category,
                            "base_year": base_year,
                            "base_month": base_month,
                        },
                        intent=IntentRef(
                            source=AnchorSource.EXPLICIT,
                            anchor_id=grant_anchor(ledger, AnchorSource.EXPLICIT, "账单分类趋势"),
                        ),
                        depends_on=("n1",),
                    )
                )
        plan = Plan(plan_id="bills_yearly" if yearly else "bills_monthly", nodes=tuple(nodes))
        validate_plan(plan)
        return plan


def _share_inputs(
    gateway: FactGateway, user_id: str, year: int, month: int, category: str
) -> tuple[int, int, str]:
    summary = category_totals(gateway, user_id, year, month)
    part = _category_expense_cents(summary, category)
    return part, summary.total_expense.cents, f"{user_id}:{year:04d}-{month:02d}"


def _category_expense_cents(summary: BillSummary, category: str) -> int:
    for item in summary.by_category:
        if item.category == category and item.total.cents < 0:
            return -item.total.cents
    raise ScenarioRefused(f"当期账单不存在支出分类：{category}")


def _capability_boundary(intents: Sequence[Intent]) -> str | None:
    """命中能力边界时返回一句人话；未命中返回 `None`（照常出计划卡）。"""
    text = " ".join(
        str(intent.slots.get("span") or intent.evidence or "") for intent in intents
    ).lower()
    for keywords, message in _CAPABILITY_BOUNDARIES:
        if any(keyword in text for keyword in keywords):
            return message
    return None


_EXPLICIT_PERIOD = re.compile(r"(?P<year>\d{4})\s*(?:年|[-/.])\s*(?P<month>\d{1,2})\s*月?")
"""原话里**明确写出**的账期：「2019 年 1 月」「2026-09」「2019/1」（年份越界由白名单拒绝）。"""

_RELATIVE_PERIOD = re.compile(r"(?P<rel>去年|今年|明年)\s*(?P<month>\d{1,2})\s*月")
"""相对年份 + 月份：「去年 12 月」。只认这三种相对词，不猜「上上个月」之类。"""

_STATED_YEAR = re.compile(r"(?P<year>\d{4})\s*年|(?P<rel>去年|今年|明年)")
"""年度账单的年份：「2019 年的年度账单」「去年的年度账单」。"""

_RELATIVE_YEAR_OFFSET = {"去年": -1, "今年": 0, "明年": 1}


def _checked_year(year: int) -> int:
    """年份白名单：与 `/api/bills/analysis` 同口径（2000–2100）。"""
    if not 2000 <= year <= 2100:
        raise ScenarioRefused(f"账期超出支持范围：{year} 年（只支持 2000–2100 年）")
    return year


def _checked_period(year: int, month: int) -> tuple[int, int]:
    """账期白名单校验：与 `/api/bills/analysis` 同口径（2000–2100 年、1–12 月）。"""
    _checked_year(year)
    if not 1 <= month <= 12:
        raise ScenarioRefused(f"账期不合法：{month} 月（应为 1–12 月）")
    return year, month


def _stated_period(intents: Sequence[Intent]) -> tuple[int, int] | None:
    """从意图原话里取**写明**的账期；没写就返回 `None`（保持「缺省 = 上一个月」）。

    只看 `span`（报文回指的原文片段），不改任何控制槽：`bills.monthly` 是纯查询，
    账期只是查询参数。解析是确定性的，零模型参与；越界一律拒绝，不静默换成本月。
    """
    for intent in intents:
        span = intent.slots.get("span")
        if not isinstance(span, str):
            continue
        match = _EXPLICIT_PERIOD.search(span)
        if match is not None:
            return _checked_period(int(match["year"]), int(match["month"]))
        match = _RELATIVE_PERIOD.search(span)
        if match is not None:
            year = DEFAULT_BOOKING_DATE.year + _RELATIVE_YEAR_OFFSET[match["rel"]]
            return _checked_period(year, int(match["month"]))
    return None


def _period(intents: Sequence[Intent]) -> tuple[int, int]:
    for intent in intents:
        if intent.action in BILLS_ACTIONS and "year" in intent.slots and "month" in intent.slots:
            year = as_int(require_trusted(intent.slots["year"], "year"), "year")
            month = as_int(require_trusted(intent.slots["month"], "month"), "month")
            return year, month
    stated = _stated_period(intents)
    if stated is not None:
        return stated
    return _previous_month(DEFAULT_BOOKING_DATE.year, DEFAULT_BOOKING_DATE.month)


def _year(intents: Sequence[Intent]) -> int:
    """年度账单的年份：取可信 `year` 槽，其次取原话写明的年份；都没有才用基准年（不猜）。"""
    for intent in intents:
        if intent.action == "bills.yearly" and "year" in intent.slots:
            return as_int(require_trusted(intent.slots["year"], "year"), "year")
    for intent in intents:
        span = intent.slots.get("span")
        if not isinstance(span, str):
            continue
        match = _STATED_YEAR.search(span)
        if match is None:
            continue
        if match["year"]:
            return _checked_year(int(match["year"]))
        return _checked_year(DEFAULT_BOOKING_DATE.year + _RELATIVE_YEAR_OFFSET[match["rel"]])
    return DEFAULT_BOOKING_DATE.year


def _previous_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _month_range(year: int, month: int) -> tuple[date, date]:
    _, last_day = calendar.monthrange(year, month)
    return date(year, month, 1), date(year, month, last_day)


def _chosen_category(intents: Sequence[Intent], summary: BillSummary) -> str:
    for intent in intents:
        if intent.action in BILLS_ACTIONS and "category" in intent.slots:
            raw = require_trusted(intent.slots["category"], "category")
            if isinstance(raw, str) and raw:
                return raw
            raise ScenarioRefused("分类取值不合法")
    expenses = [item for item in summary.by_category if item.total.cents < 0]
    if not expenses:
        raise ScenarioRefused("当期账单无可分析支出，拒绝生成趋势节点")
    top = max(expenses, key=lambda item: (-item.total.cents, item.category))
    return top.category


def _account_balance(context: QueryContext) -> QueryResult:
    record = context.gateway.account(require_str(context.params, "account_id"))
    return QueryResult(
        record_ids=(record.record_id,),
        provenance=((record.record_id, record.content_hash),),
        detail=f"账户 {record.payload.account_id} 余额 {record.payload.balance.yuan} 元",
    )


def _account_transactions(context: QueryContext) -> QueryResult:
    account_id = require_str(context.params, "account_id")
    rows = context.gateway.transactions_between(
        account_id,
        require_date(context.params, "start"),
        require_date(context.params, "end"),
    )
    return QueryResult(
        record_ids=tuple(item.record_id for item in rows),
        provenance=tuple((item.record_id, item.content_hash) for item in rows),
        detail=f"账户 {account_id} 区间交易 {len(rows)} 笔",
    )


def _monthly_bill(context: QueryContext) -> QueryResult:
    year = require_int(context.params, "year")
    month = require_int(context.params, "month")
    summary = context.gateway.monthly_bill(
        require_str(context.params, "user_id"), year, month
    )
    return QueryResult(
        record_ids=(summary.record_id,),
        provenance=((summary.record_id, summary.content_hash),),
        detail=(
            f"{year}-{month:02d} 支出 {summary.payload.total_expense.yuan} 元"
            f" / 收入 {summary.payload.total_income.yuan} 元"
        ),
    )


def _category_trend(context: QueryContext) -> QueryResult:
    summary = context.gateway.monthly_bill(
        require_str(context.params, "user_id"),
        require_int(context.params, "base_year"),
        require_int(context.params, "base_month"),
    )
    category = require_str(context.params, "category")
    total = Money(0)
    for item in summary.payload.by_category:
        if item.category == category:
            total = item.total
    return QueryResult(
        record_ids=(summary.record_id,),
        provenance=((summary.record_id, summary.content_hash),),
        detail=f"分类 {category} 基期合计 {total.yuan} 元",
    )


def ytd_end_month(year: int) -> int:
    """同期同比的**基准月**：只来自唯一基准时钟（`scenarios.scheduling.REFERENCE_DATE`）。

    基准时钟为 `2026-10-01`，其**所在月的前一个月**（`2026-09`）即最近一个完整账期，
    与 `_period` 的缺省账期口径一致。**仅当请求年份等于基准年时**取 YTD（`1..基准月`）；
    其余年份按**整年**（`1..12`，历史完整年），否则口径不可比。
    """
    if year == REFERENCE_DATE.year:
        return _previous_month(REFERENCE_DATE.year, REFERENCE_DATE.month)[1]
    return 12


def _yearly_bill(context: QueryContext) -> QueryResult:
    """年度账单（P2-1 加固）：**同期（YTD）同比**。

    本金区间 = 本年 `1 月 .. 基准月`，对照区间 = 上年 `1 月 .. 同月`；两个区间的月度账单事实
    **各自只走 `sum` 算子**，同比走 `yoy` 算子（H1 事实零生成，出处携带两个区间全部
    `record_id`）。基准月只来自 `scenarios.scheduling.REFERENCE_DATE`。

    **两种「不可比」区分对待**：
    - 上年同期**完全没有任何流水事实**（月度账单事实的笔数合计 = 0）⇒ **不调用算子**，
      如实声明「上年同期无可比数据」；
    - 上年同期**有事实但支出合计 = 0** ⇒ 照走算子，由算子拒绝（除零）后如实输出「不可判」。
    """
    user_id = require_str(context.params, "user_id")
    year = require_int(context.params, "year")
    end_month = ytd_end_month(year)
    months = tuple(range(1, end_month + 1))
    this_year = tuple(context.gateway.monthly_bill(user_id, year, month) for month in months)
    last_year = tuple(
        context.gateway.monthly_bill(user_id, year - 1, month) for month in months
    )
    records = (*this_year, *last_year)
    sandbox_events: list[dict[str, object]] = []
    monitor_sink = context.sandbox_monitor
    last_fact_count = sum(
        item.txn_count for record in last_year for item in record.payload.by_category
    )
    try:
        expense = _annual_sum(
            this_year,
            lambda bill: bill.total_expense.cents,
            use="current_year_expense",
            events=sandbox_events,
            monitor_sink=monitor_sink,
            code_generator=context.analysis_code_generator,
        )
        income = _annual_sum(
            this_year,
            lambda bill: bill.total_income.cents,
            use="current_year_income",
            events=sandbox_events,
            monitor_sink=monitor_sink,
            code_generator=context.analysis_code_generator,
        )
        last_expense = _annual_sum(
            last_year,
            lambda bill: bill.total_expense.cents,
            use="prior_year_expense",
            events=sandbox_events,
            monitor_sink=monitor_sink,
            code_generator=context.analysis_code_generator,
        )
        yoy: SandboxResult | None = None
        yoy_failed = False
        if last_fact_count:
            try:
                yoy = _run_bill_analysis(
                    AnalysisTask.YOY,
                    {
                        "current": [
                            {
                                "record_id": record.record_id,
                                "value": str(record.payload.total_expense.cents),
                            }
                            for record in this_year
                        ],
                        "prior": [
                            {
                                "record_id": record.record_id,
                                "value": str(record.payload.total_expense.cents),
                            }
                            for record in last_year
                        ],
                    },
                    use="year_over_year",
                    events=sandbox_events,
                    monitor_sink=monitor_sink,
                    code_generator=context.analysis_code_generator,
                )
            except OperatorError:
                # 既有算子会对上年同期支出为零拒绝除法。
                yoy_failed = True
    except (AnalysisCodeGenerationError, SandboxError):
        return QueryResult(
            record_ids=tuple(item.record_id for item in records),
            provenance=tuple((item.record_id, item.content_hash) for item in records),
            detail="年度账单分析沙箱拒绝或无法执行；本次未生成年度数值。",
            sandbox_events=tuple(sandbox_events),
        )

    detail = (
        f"{year} 年 1–{end_month} 月支出 {expense.yuan} 元 / 收入 {income.yuan} 元"
        f"（{len(this_year)} 个月度账单事实，同期同比口径）"
    )
    if last_fact_count == 0:
        detail += f"；上年同期（{year - 1} 年 1–{end_month} 月）无可比数据，同比不可判"
    elif yoy_failed:
        detail += (
            f"；上年同期（{year - 1} 年 1–{end_month} 月）支出合计为 0，"
            "白名单算子拒绝除以零，同比不可判"
        )
    else:
        assert yoy is not None
        detail += (
            f"；较去年同期（上年同期支出 {last_expense.yuan} 元）"
            f"{_signed_percent(yoy.value)}"
        )
    return QueryResult(
        record_ids=tuple(item.record_id for item in records),
        provenance=tuple((item.record_id, item.content_hash) for item in records),
        detail=detail,
        sandbox_events=tuple(sandbox_events),
    )


def _signed_percent(ratio: Decimal) -> str:
    """把算子产出的比率**格式化**成带符号百分比（只做渲染，不生成事实）。"""
    rendered = f"{(ratio * 100).quantize(Decimal('0.01'))}"
    return f"+{rendered}%" if ratio >= 0 else f"{rendered}%"


def _annual_sum(
    records: tuple[Record[BillSummary], ...],
    pick: Callable[[BillSummary], int],
    *,
    use: str,
    events: list[dict[str, object]],
    monitor_sink: SandboxMonitorSink | None = None,
    code_generator: Callable[[str, tuple[str, ...]], str] | None = None,
) -> Money:
    """年度合计：由受限分析代码执行白名单算子并携带全部出处。"""
    derived = _run_bill_analysis(
        AnalysisTask.SUM,
        {
            "facts": [
                {"record_id": record.record_id, "value": str(pick(record.payload))}
                for record in records
            ]
        },
        use=use,
        events=events,
        monitor_sink=monitor_sink,
        code_generator=code_generator,
    )
    return Money(int(derived.value))


def _run_bill_analysis(
    task: AnalysisTask,
    snapshot: Mapping[str, object],
    *,
    use: str,
    events: list[dict[str, object]],
    monitor_sink: SandboxMonitorSink | None = None,
    code_generator: Callable[[str, tuple[str, ...]], str] | None = None,
) -> SandboxResult:
    """只把任务/字段名交给生成器；事实快照仅传入父进程校验后的沙箱请求。"""
    captured_monitors: list[SandboxMonitor] = []

    def capture(monitor: SandboxMonitor) -> None:
        captured_monitors.append(monitor)
        if monitor_sink is not None:
            monitor_sink(monitor)

    try:
        code = (
            code_generator(task.value, tuple(snapshot))
            if code_generator is not None
            else generate_analysis_code(task, tuple(snapshot))
        )
        result = run_generated_analysis(code, snapshot, monitor_sink=capture)
    except Exception as error:
        monitor = getattr(error, "monitor", None)
        if isinstance(monitor, Mapping):
            event = dict(monitor)
        elif captured_monitors:
            event = asdict(captured_monitors[-1])
        else:
            event = {}
        if event:
            event.update({"task": task.value, "use": use, "result": None})
            event["failure"] = type(error).__name__
            events.append(event)
        raise
    if result.monitor is not None:
        event = asdict(result.monitor)
        event.update(
            {
                "task": task.value,
                "use": use,
                "result": {
                    "operator": result.operator,
                    "value": str(result.value),
                    "sources": list(result.sources),
                },
            }
        )
        events.append(event)
    return result


QUERY_HANDLERS: Mapping[str, QueryHandler] = {
    "account.balance": _account_balance,
    "account.transactions": _account_transactions,
    "bills.monthly": _monthly_bill,
    "bills.category_trend": _category_trend,
    "bills.yearly": _yearly_bill,
}
"""账单域**读**处理器（组合根汇入 PEP；`docs/已修复清单-2026-10-08.md` 步骤 4「残留」补做）。"""
