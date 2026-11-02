"""计划卡：**确定性模板**渲染（输入 = 已裁决 DAG + 事实网关数值）。

铁律（PRD §5.4）：
- 计划卡与执行计划**共用同一份已裁决 DAG**（结构上不可能不一致）；
- 数值只来自 `PlanNode.params` 与 `FactGateway`（H1 事实零生成），本层**不生成**任何数字；
- 计划卡只回显**动作 / 对象 / 时间三元组**（摘要另加 `web/present.py` 白名单里的事实行），
  **不回显数据槽原文**（备注里的注入文本不进卡片，H3）；
- LLM 只允许做**不改变三元组**的润色，由 `polish_plan_card` 的确定性守卫强制
  （丢对象 / 改行数即拒）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Protocol

from ai_banking_copilot.contracts.authorization import Decision, Level
from ai_banking_copilot.contracts.plan import Plan, PlanNode, cfhash, validate_plan
from ai_banking_copilot.gateway.errors import GatewayNotFoundError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Money
from ai_banking_copilot.planning.kernel import resource_key
from ai_banking_copilot.scenarios import bills, subscriptions, wealth
from ai_banking_copilot.web.present import fact_lines

PROMPT_USER_CONFIRM = "user_confirm"
"""黄色：用户确认。"""

PROMPT_STRONG_VERIFY = "strong_verify"
"""红色：强验证（服务端一次性验证码挑战）。"""

PROMPT_MANUAL_TAKEOVER = "manual_takeover"
"""增强层 L3：人工接管。"""

PROMPT_DENY = "deny_and_explain"
"""增强层 L4：拒绝并解释。"""

_ACTION_LABELS: dict[str, str] = {
    "account.balance": "余额查询",
    "account.transactions": "交易记录查询",
    "bills.monthly": "月度账单分析",
    "bills.category_trend": "分类趋势分析",
    "bills.yearly": "年度账单汇总",
    "wealth.list": "理财查询",
    "card.list": "卡片查询",
    "subscription.list": "代扣协议查询",
    "transfer.create": "转账",
    "transfer.schedule": "定时转账（立即扣款，指定记账日）",
    "transfer.book": "登记定时转账（本次不扣款）",
    "transfer.request_collection": "发起 AA 收款",
    "wealth.subscribe": "理财申购",
    "wealth.redeem": "理财赎回",
    "subscription.cancel": "关闭扣款协议",
    "subscription.sign": "代扣签约",
    "card.lock": "临时冻结（口头挂失）",
    "card.unlock": "解挂",
    "card.report_lost": "正式挂失",
    "card.replace": "补卡（换新卡号）",
    "card.apply_virtual": "申请虚拟卡",
    "card.set_limit": "调整借记卡通道限额",
    "card.adjust_credit_limit": "调整信用卡额度",
    "card.set_cycle_limit": "设置账单周期消费限额",
    "password.change": "修改交易密码",
    "funds.freeze": "锁定预算（资金预留）",
    "funds.release": "释放预留资金",
    "merchant.order": "向商户下单",
}

_CARD_TYPE_LABELS = {
    "debit": "招商银行借记卡",
    "credit": "招商银行信用卡",
    "virtual": "招商银行虚拟卡",
}
"""演示卡名：真实银行名称只用于展示，界面上与「演示环境」标注同时出现。"""


@dataclass(frozen=True)
class PlanTriple:
    """一个节点的**关键意图三元组**（动作 / 对象 / 时间）——润色不得改动它。"""

    action: str
    object_label: str
    when_label: str


@dataclass(frozen=True)
class PlanLine:
    """结构化计划卡的一行（键值对）。`lines` 的纯文本版原样保留。"""

    label: str
    value: str
    emphasis: bool = False
    """关键行（金额）加粗；由渲染层决定视觉，不承载判定。"""

    def to_dict(self) -> dict[str, object]:
        return {"label": self.label, "value": self.value, "emphasis": self.emphasis}


@dataclass(frozen=True)
class PlanCard:
    """计划卡：`requires` 由 `level` 单向推导，禁止外部自由填写。"""

    card_id: str
    frozen_hash: str
    level: Level
    lines: tuple[str, ...]
    requires: tuple[str, ...]
    expires_at: datetime | None = None
    """服务端下发的过期时刻（G-02）：过期后原计划不得再被确认；`None` = 未设置。"""

    balance_trace: tuple[str, ...] = ()
    """预算冻结类计划的**可用余额轨迹**（冻结 → 扣款 → 释放），逐节点给出执行后可用余额。

    与 `lines` 分离：`lines` 是受润色守卫约束的三元组文案（行数不得变），轨迹是纯追加的
    事实回显，不参与润色。非冻结类计划为空。
    """

    summary: tuple[PlanLine, ...] = ()
    """结构化摘要（动作 / 金额 / 对象 / 时间），供前端优先渲染。

    与 `lines` 同源同值，只是拆成键值对；润色只改 `lines`，不改本字段。
    """

    impact: tuple[str, ...] = ()
    """**连带影响**（只读事实）：卡/账户上已有的代扣、预约、冻结。

    只描述"当前库里真实存在的关联事实"，关系列缺失时明说"未知"，不按 0 计。
    """

    def __post_init__(self) -> None:
        if not self.card_id:
            raise ValueError("card_id 不得为空")
        if len(self.frozen_hash) != 64:
            raise ValueError("frozen_hash 必须是 64 位十六进制")
        if not isinstance(self.level, Level):
            raise TypeError(f"level 必须是 Level 成员，收到 {type(self.level).__name__}")
        if self.requires != _requires_for_level(self.level):
            raise ValueError("requires 必须由 level 推导，不得自由填写")


class PlanCardPolisher(Protocol):
    """润色通道（通常是 LLM）：只接收与返回文案，**不接触**计划与数值。"""

    def polish(self, lines: tuple[str, ...]) -> tuple[str, ...]:
        """返回润色后的同序文案行。"""
        ...


class PolishRejectedError(ValueError):
    """润色试图改变动作 / 对象 / 时间三元组或行数：拒绝并保留确定性模板。"""


def render_plan_card(
    plan: Plan, decision: Decision, gateway: FactGateway, *,
    expires_at: datetime | None = None,
    user_id: str | None = None,
) -> PlanCard:
    """用确定性模板渲染计划卡（输入 = 已裁决 DAG + 事实网关数值）。

    `expires_at` 由**服务端**在生成计划卡时写入，不进入冻结哈希（卡片元数据）。
    `user_id` 只用于查**连带影响**（卡上的代扣 / 账户上的预约），缺省不展示。
    """
    validate_plan(plan)
    frozen_hash = cfhash(plan.nodes)
    triples = plan_triples(plan, gateway)
    lines = tuple(
        f"{index}. {_ACTION_LABELS.get(node.action, node.action)}："
        f"{triple.object_label}{_amount_label(node)}（{triple.when_label}）"
        for index, (node, triple) in enumerate(zip(plan.nodes, triples, strict=True), start=1)
    )
    return PlanCard(
        card_id=f"card_{frozen_hash[:12]}_{decision.level.name.lower()}",
        frozen_hash=frozen_hash,
        level=decision.level,
        lines=lines,
        requires=_requires_for_level(decision.level),
        expires_at=expires_at,
        balance_trace=_balance_trace(plan, gateway),
        summary=plan_summary(plan, gateway),
        impact=_impact_lines(plan, gateway, user_id),
    )


_TRACE_ACTIONS = frozenset({"funds.freeze", "funds.release", "merchant.order", "transfer.create"})
"""余额轨迹覆盖的资金动作：冻结 / 解冻 / 冻结额度内扣款 / 转账。"""

_TRANSFER_METHOD_LABELS: dict[str, str] = {
    "realtime": "实时到账",
    "normal": "普通到账",
    "next_day": "次日到账",
    "large": "大额通道",
}

_TRANSFER_ETA_LABELS: dict[str, str] = {
    "realtime": "7×24 小时，预计秒到（演示即时记账）",
    "normal": "预计 2 小时内到账（演示即时记账）",
    "next_day": "次日 00:00 后发出，到账前可撤销",
    "large": "工作日大额通道处理，周末与节假日顺延（演示环境按强验证处理）",
}

_TRANSFER_PURPOSE_LABELS: dict[str, str] = {
    "rent": "房租",
    "salary": "工资",
    "reimburse": "报销",
    "other": "其他",
}

_TRANSFER_ACTIONS = frozenset({"transfer.create", "transfer.schedule", "transfer.book"})

_TRACE_LABELS: dict[str, str] = {
    "funds.freeze": "预留",
    "funds.release": "释放",
    "merchant.order": "扣款",
    "transfer.create": "转账",
}


def _balance_trace(plan: Plan, gateway: FactGateway) -> tuple[str, ...]:
    """预算冻结类计划的可用余额轨迹（其余计划返回空）。

    数值只来自 `PlanNode.params` 与 `FactGateway`，本函数**不生成**任何数字；`merchant.order`
    从冻结额度结算，可用余额在冻结时已扣减，因此只标注扣款额而不再次扣减。
    """
    if not any(node.action in ("funds.freeze", "funds.release") for node in plan.nodes):
        return ()
    account_id = _trace_account_id(plan, gateway)
    if account_id is None:
        return ()
    try:
        available = gateway.account(account_id).payload.balance.cents
    except GatewayNotFoundError:
        return ()
    trace = [f"执行前可用余额 {Money(available).yuan} 元"]
    for node in plan.nodes:
        if node.action not in _TRACE_ACTIONS:
            continue
        cents = node.params.get("amount_cents")
        if not isinstance(cents, int) or isinstance(cents, bool):
            continue
        if node.action == "funds.freeze":
            available -= cents
        elif node.action == "funds.release":
            available += cents
        elif node.action == "transfer.create":
            available -= cents
        note = "（从预留额度结算）" if node.action == "merchant.order" else ""
        trace.append(
            f"{_TRACE_LABELS[node.action]} {Money(cents).yuan} 元{note}"
            f" → 可用 {Money(available).yuan} 元"
        )
    return tuple(trace)


def _trace_account_id(plan: Plan, gateway: FactGateway) -> str | None:
    """轨迹账户：优先取节点自带 `account_id`，否则由冻结记录反查。"""
    for node in plan.nodes:
        account_id = _str_param(node, "account_id")
        if account_id is not None:
            return account_id
    for node in plan.nodes:
        hold_id = _str_param(node, "hold_id") or _str_param(node, "budget_hold_id")
        if hold_id is None:
            continue
        try:
            return gateway.hold(hold_id).payload.account_id
        except GatewayNotFoundError:
            return None
    return None


def plan_triples(plan: Plan, gateway: FactGateway) -> tuple[PlanTriple, ...]:
    """逐节点抽出动作 / 对象 / 时间三元组（对象与时间取自事实网关）。"""
    return tuple(
        PlanTriple(
            action=node.action,
            object_label=_object_label(node, gateway),
            when_label=_when_label(node, gateway),
        )
        for node in plan.nodes
    )


def plan_summary(plan: Plan, gateway: FactGateway) -> tuple[PlanLine, ...]:
    """把三元组拆成结构化键值对（单节点）或逐步摘要（多节点）。

    数值一律复用 `_amount_value`（与 `lines` 同源），不新增计算口径。
    既有摘要在前；其后追加 `web/present.py` 白名单里的事实行（最多 6 条，内部键已被剔除）。
    追加前做**值去重**：值已出现在既有摘要的 `value` 里（如 `"99.00"` 命中 `"99.00 元"`）
    就跳过，避免商户 / 金额 / 日期在卡上重复两遍。
    """
    triples = plan_triples(plan, gateway)
    existing: list[PlanLine]
    if len(plan.nodes) == 1:
        node = plan.nodes[0]
        triple = triples[0]
        rows = [PlanLine("动作", _ACTION_LABELS.get(node.action, node.action))]
        amount = _amount_value(node)
        if amount:
            rows.append(PlanLine("金额", amount, emphasis=True))
        rows.append(PlanLine("对象", triple.object_label))
        if triple.when_label != "—":
            rows.append(PlanLine("时间", triple.when_label))
        existing = rows
    else:
        existing = [
            PlanLine(
                f"{index}. {_ACTION_LABELS.get(node.action, node.action)}",
                _step_value(node, triple),
            )
            for index, (node, triple) in enumerate(zip(plan.nodes, triples, strict=True), start=1)
        ]
    if len(plan.nodes) == 1 and plan.nodes[0].action in _TRANSFER_ACTIONS:
        existing.extend(_transfer_account_lines(plan.nodes[0], gateway))
        existing.extend(_transfer_method_lines(plan.nodes[0]))
    seen = [line.value for line in existing]
    extra: list[PlanLine] = []
    for node in plan.nodes:
        for fact in fact_lines(node.action, node.params):
            if any(fact.value in value for value in seen):
                continue
            extra.append(PlanLine(fact.label, fact.value, emphasis=fact.emphasis))
            seen.append(fact.value)
    return (*existing, *extra[:6])


def _step_value(node: PlanNode, triple: PlanTriple) -> str:
    amount = _amount_value(node)
    head = f"{triple.object_label} {amount}" if amount else triple.object_label
    return f"{head}（{triple.when_label}）"


_ACCOUNT_KIND_LABELS: dict[str, str] = {
    "savings": "储蓄",
    "current": "活期",
}


def _transfer_account_lines(node: PlanNode, gateway: FactGateway) -> list[PlanLine]:
    """付款账户事实：只回显网关的账户类型与可用余额，不补卡号。"""
    account_id = _str_param(node, "account_id")
    if account_id is None:
        return []
    try:
        account = gateway.account(account_id).payload
    except GatewayNotFoundError:
        return []
    kind = _ACCOUNT_KIND_LABELS.get(account.kind, account.kind)
    return [PlanLine("付款账户", f"{kind}账户 · 可用 {account.balance.yuan:,.2f} 元")]


def _transfer_method_lines(node: PlanNode) -> list[PlanLine]:
    """转账计划卡的人话事实：方式、到账时间、手续费、用途。数字不在这里生成。"""
    raw_method = node.params.get("transfer_method")
    method = raw_method if isinstance(raw_method, str) and raw_method else (
        "next_day" if node.action in {"transfer.book", "transfer.schedule"} else "realtime"
    )
    method_label = _TRANSFER_METHOD_LABELS.get(method, "实时到账")
    if node.action == "transfer.book":
        method_label = "次日到账（登记）"
    if node.action == "transfer.schedule":
        method_label = "定时转账"
    eta = (
        "按约定记账日处理（立即扣款）"
        if node.action == "transfer.schedule"
        else _TRANSFER_ETA_LABELS.get(method, "以银行为准")
    )
    rows = [
        PlanLine("转账方式", method_label),
        PlanLine("到账时间", eta),
        PlanLine("手续费", "¥0.00（演示免收）"),
    ]
    purpose = node.params.get("purpose")
    if isinstance(purpose, str) and purpose:
        rows.append(PlanLine("用途", _TRANSFER_PURPOSE_LABELS.get(purpose, purpose)))
    return rows


def polish_plan_card(
    card: PlanCard, triples: tuple[PlanTriple, ...], polisher: PlanCardPolisher
) -> PlanCard:
    """应用润色并**强制三元组不变**：丢对象 / 丢时间 / 改行数一律拒绝（fail-safe 保留模板）。"""
    if len(triples) != len(card.lines):
        raise PolishRejectedError("三元组与文案行数不一致，拒绝润色")
    polished = polisher.polish(card.lines)
    if len(polished) != len(card.lines):
        raise PolishRejectedError(f"润色改变了行数：{len(card.lines)} -> {len(polished)}")
    for index, (line, triple) in enumerate(zip(polished, triples, strict=True), start=1):
        if triple.object_label not in line:
            raise PolishRejectedError(f"第 {index} 行丢失对象三元组：{triple.object_label}")
        if triple.when_label != "—" and triple.when_label not in line:
            raise PolishRejectedError(f"第 {index} 行丢失时间三元组：{triple.when_label}")
    return replace(card, lines=polished)


def describe_decision(decision: Decision) -> tuple[str, ...]:
    """可机读的命中规则列表（对齐 PRD §9.1 的「命中规则 id」）。"""
    return tuple(reason.value for reason in decision.reasons)


def _requires_for_level(level: Level) -> tuple[str, ...]:
    if level is Level.GREEN:
        return ()
    if level is Level.YELLOW:
        return (PROMPT_USER_CONFIRM,)
    if level is Level.RED:
        return (PROMPT_USER_CONFIRM, PROMPT_STRONG_VERIFY)
    if level is Level.MANUAL:
        return (PROMPT_MANUAL_TAKEOVER,)
    return (PROMPT_DENY,)


def _object_label(node: PlanNode, gateway: FactGateway) -> str:
    if node.action == "password.change":
        return "本人交易密码"
    if node.action == "account.balance":
        account_id = _str_param(node, "account_id")
        if account_id is not None:
            balance = gateway.account(account_id).payload.balance
            return f"账户 {account_id} · 余额 {balance.yuan} 元"
    if node.action in ("subscription.sign", "merchant.order"):
        merchant = _str_param(node, "merchant") or "商户"
        if node.action == "subscription.sign":
            period = {"monthly": "每月", "yearly": "每年"}.get(
                _str_param(node, "period") or "", ""
            )
            return f"{merchant} · {period}"
        return merchant
    if node.action == "wealth.list":
        risk_profile = _str_param(node, "risk_profile")
        if risk_profile is not None:
            labels = wealth.candidate_labels(gateway, risk_profile)
            return f"理财产品（{risk_profile} 适配）：{'；'.join(labels)}"
        return f"理财产品：{'；'.join(wealth.product_comparison(gateway))}"
    if node.action == "subscription.list":
        user_id = _str_param(node, "user_id")
        if user_id is not None:
            return subscriptions.describe_agreements_facts(gateway, user_id)
    if node.action == "account.transactions":
        account_id = _str_param(node, "account_id")
        start = _str_param(node, "start")
        end = _str_param(node, "end")
        if account_id is not None and start is not None and end is not None:
            large = bills.large_transactions(
                gateway, account_id, date.fromisoformat(start), date.fromisoformat(end)
            )
            return f"账户 {account_id}：{bills.describe_large_transactions(large)}"
    payee_id = _str_param(node, "payee_id")
    if payee_id is not None:
        try:
            payee = gateway.payee(payee_id).payload
        except GatewayNotFoundError:
            return f"收款人 {payee_id}"
        bank = f" · {payee.bank_name}" if payee.bank_name else ""
        return f"{payee.full_name}（账号尾号 {payee.account_tail}{bank}）"
    product_id = _str_param(node, "product_id")
    if product_id is not None:
        try:
            product = gateway.wealth_product(product_id).payload
        except GatewayNotFoundError:
            return f"理财产品 {product_id}"
        return f"{product.name}（{product.risk_level}）"
    if node.action == "subscription.cancel":
        subscription_id = _str_param(node, "subscription_id")
        if subscription_id is not None:
            try:
                subscription = gateway.subscription(subscription_id).payload
            except GatewayNotFoundError:
                return f"代扣协议 {subscription_id}"
            return subscriptions.describe_agreement_facts(
                gateway, subscription.user_id, subscription_id
            )
    subscription_id = _str_param(node, "subscription_id")
    if subscription_id is not None:
        try:
            subscription = gateway.subscription(subscription_id).payload
        except GatewayNotFoundError:
            return f"订阅 {subscription_id}"
        return subscription.merchant
    card_id = _str_param(node, "card_id")
    if card_id is not None:
        try:
            card = gateway.card(card_id).payload
        except GatewayNotFoundError:
            return f"卡片 {card_id}"
        return f"{_CARD_TYPE_LABELS.get(card.card_type, '银行卡')} · 尾号 {card.last4 or '未知'}"
    hold_id = _str_param(node, "hold_id")
    if hold_id is not None:
        return f"冻结记录 {hold_id}"
    account_id = _str_param(node, "account_id")
    if account_id is not None:
        return f"账户 {account_id}"
    return "本人账户"


def _when_label(node: PlanNode, gateway: FactGateway) -> str:
    for name in ("executed_on", "next_charge_on", "order_on"):
        when = _str_param(node, name)
        if when is not None:
            return when
    if node.action == "account.transactions":
        start = _str_param(node, "start")
        end = _str_param(node, "end")
        if start is not None and end is not None:
            return f"{start}~{end}"
    if node.action == "bills.monthly":
        # 账期是计划参数：计划卡必须把它写出来，否则「查 2019 年 1 月」和「查本月」
        # 在卡上长得一模一样，用户无法在确认前核对查的是哪个月。
        year = node.params.get("year")
        month = node.params.get("month")
        if (
            isinstance(year, int) and not isinstance(year, bool)
            and isinstance(month, int) and not isinstance(month, bool)
        ):
            return f"{year}-{month:02d}"
        return "—"
    if node.action == "bills.yearly":
        year = node.params.get("year")
        if isinstance(year, int) and not isinstance(year, bool):
            return f"{year}-01~{year}-{bills.ytd_end_month(year):02d}"
        return "—"
    if node.action == "subscription.list":
        user_id = _str_param(node, "user_id")
        if user_id is not None:
            upcoming = subscriptions.upcoming_charges(gateway, user_id)
            if upcoming:
                return upcoming[0].next_charge_on.isoformat()
            return "—"
    booked_on = _str_param(node, "booked_on")
    if booked_on is not None:
        return booked_on
    subscription_id = _str_param(node, "subscription_id")
    if subscription_id is not None:
        try:
            return gateway.subscription(subscription_id).payload.next_charge_on.isoformat()
        except GatewayNotFoundError:
            return "—"
    return "—"


def _amount_label(node: PlanNode) -> str:
    cents = node.params.get("amount_cents")
    if isinstance(cents, int) and not isinstance(cents, bool):
        currency = _str_param(node, "currency")
        suffix = f"（{currency.upper()}）" if currency is not None else ""
        return f" 金额 {Money(cents).yuan} 元{suffix}"
    limit = node.params.get("daily_limit_cents")
    if isinstance(limit, int) and not isinstance(limit, bool):
        return f" 额度 {Money(limit).yuan} 元"
    return ""


def _amount_value(node: PlanNode) -> str:
    """金额 / 额度的事实值（无则为空串）；与 `_amount_label` 同源同值。"""

    cents = node.params.get("amount_cents")
    if isinstance(cents, int) and not isinstance(cents, bool):
        currency = _str_param(node, "currency")
        suffix = f"（{currency.upper()}）" if currency is not None else ""
        return f"{Money(cents).yuan} 元{suffix}"
    limit = node.params.get("daily_limit_cents")
    if isinstance(limit, int) and not isinstance(limit, bool):
        return f"{Money(limit).yuan} 元"
    return ""


def _str_param(node: PlanNode, key: str) -> str | None:
    value = node.params.get(key)
    if isinstance(value, str) and value:
        return value
    return None


_RESOURCE_LABELS: dict[str, str] = {
    "account_id": "同一账户",
    "card_id": "同一张卡",
    "subscription_id": "同一订阅",
    "hold_id": "同一笔冻结",
    "merchant": "同一商户",
}
"""资源键参数 → 中文标签（只用于**解释为什么串行**，不参与任何判定）。"""


def _resource_label(node: PlanNode) -> str:
    key = resource_key(node)
    if key is None:
        return ""
    param = key.split(":", 1)[0]
    return _RESOURCE_LABELS.get(param, "同一对象")


def plan_nodes_payload(plan: Plan | None) -> list[dict[str, object]]:
    """把**冻结计划**投影成前端可渲染的节点数组。

    只回显服务端已有字段（`node_id` / `action` / `depends_on` / 资源键），
    依赖与资源键均由规划内核机械推导——前端不推导、不补节点、不改顺序。
    """
    if plan is None:
        return []
    rows: list[dict[str, object]] = []
    for index, node in enumerate(plan.nodes, start=1):
        rows.append({
            "node_id": node.node_id,
            "action": node.action,
            "label": _ACTION_LABELS.get(node.action, node.action),
            "depends_on": list(node.depends_on),
            "resource": resource_key(node) or "",
            "resource_label": _resource_label(node),
            "index": index,
            # 卡片弹窗需要按卡定位：只透出计划里已裁决的 card_id。
            "card_id": node.params.get("card_id")
            if isinstance(node.params.get("card_id"), str)
            else "",
            # 理财确认弹窗需要预填：只透出已裁决计划里的产品 / 金额 / 份额事实。
            "product_id": node.params.get("product_id")
            if isinstance(node.params.get("product_id"), str)
            else "",
            "amount_cents": node.params.get("amount_cents")
            if isinstance(node.params.get("amount_cents"), int)
            and not isinstance(node.params.get("amount_cents"), bool)
            else None,
            "share_bp": node.params.get("share_bp")
            if isinstance(node.params.get("share_bp"), int)
            and not isinstance(node.params.get("share_bp"), bool)
            else None,
        })
    return rows


_IMPACT_CARD_ACTIONS = frozenset({"card.report_lost", "card.lock"})
"""不可逆 / 半不可逆的卡动作：预演它会让哪些**已有关系**受影响。"""

_IMPACT_MONEY_ACTIONS = frozenset({"transfer.create", "funds.freeze"})
"""动钱动作：预演该账户上已有的预约与冻结。"""

_MAX_IMPACT_LINES = 4

_PERIOD_LABELS = {"monthly": "月", "yearly": "年"}


def _impact_lines(
    plan: Plan, gateway: FactGateway, user_id: str | None
) -> tuple[str, ...]:
    """连带影响：**只陈述库里已有的关系事实**，不做因果推测。

    - 卡：该卡绑定几个在扣订阅、关联账户还有几笔未到期预约；
    - 账户：还有几笔未到期预约、几笔未完成冻结（合计金额）。
    - 关系列缺失（旧库 / 历史数据）时如实说「没有登记」，**不得当成 0 条关系**。
    """
    if user_id is None:
        return ()
    lines: list[str] = []
    for node in plan.nodes:
        if node.action in _IMPACT_CARD_ACTIONS:
            lines.extend(_card_impact(node, gateway))
        elif node.action in _IMPACT_MONEY_ACTIONS:
            lines.extend(_account_impact(node, gateway, user_id))
    unique: list[str] = []
    for line in lines:
        if line not in unique:
            unique.append(line)
    return tuple(unique[:_MAX_IMPACT_LINES])


def _card_impact(node: PlanNode, gateway: FactGateway) -> tuple[str, ...]:
    card_id = _str_param(node, "card_id")
    if card_id is None:
        return ()
    try:
        card = gateway.card(card_id).payload
    except GatewayNotFoundError:
        return ()
    lines: list[str] = []
    subscriptions = [
        record.payload
        for record in gateway.subscriptions_of(card.user_id)
        if record.payload.card_id == card_id and record.payload.status == "active"
    ]
    if subscriptions:
        detail = "、".join(
            f"{item.merchant} {item.amount.yuan} 元/"
            f"{_PERIOD_LABELS.get(item.period, item.period)}"
            for item in subscriptions[:2]
        )
        more = f" 等 {len(subscriptions)} 个" if len(subscriptions) > 2 else ""
        lines.append(
            f"该卡绑定 {len(subscriptions)} 个在扣订阅：{detail}{more}；"
            "本操作不会自动取消商户侧代扣"
        )
        lines.append("挂失不可撤销；如需继续用卡，可以申请补卡（虚拟卡）")
    if card.account_id is None:
        lines.append("这张卡没有登记绑定的账户与代扣关系，挂失后我们无法自动替您核对受影响的扣款")
        lines.append("如需继续用卡，可以申请补卡（虚拟卡）")
        return tuple(lines)
    pending = [
        record
        for record in gateway.scheduled_transfers_of(card.user_id, status="scheduled")
        if record.payload.account_id == card.account_id
    ]
    if pending:
        lines.append(f"该卡绑定的账户还有 {len(pending)} 笔未到期预约转账")
    lines.append("挂失不可撤销；如需继续用卡，可以申请补卡（虚拟卡）")
    return tuple(lines)


def _account_impact(
    node: PlanNode, gateway: FactGateway, user_id: str
) -> tuple[str, ...]:
    account_id = _str_param(node, "account_id")
    if account_id is None:
        return ()
    lines: list[str] = []
    pending = [
        record
        for record in gateway.scheduled_transfers_of(user_id, status="scheduled")
        if record.payload.account_id == account_id
    ]
    if pending:
        lines.append(f"该账户还有 {len(pending)} 笔未到期预约转账")
    holds = [
        record.payload
        for record in gateway.holds_of(account_id)
        if record.payload.status == "active"
    ]
    if holds:
        total = sum(item.amount.cents for item in holds)
        lines.append(
            f"该账户当前有 {len(holds)} 笔未完成冻结（合计 {Money(total).yuan} 元）"
        )
    return tuple(lines)
