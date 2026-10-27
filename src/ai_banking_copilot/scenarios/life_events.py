"""生活事件原型 L1–L7：口语 → 原型 → 计划（**不枚举场景，枚举链路的形状**）。

口径（`docs/2026-10-27/分工文档-2026-10-27.md` §2 / §5）：
- **七条原型** = 时间形态（A1–A4）× 资金形态（B1–B5）× 风险形态（C1–C4）压出来的链路骨架；
  同一原型换参数即成新场景（生日 → 搬家 → 开学），动作序列与三色判定一字不改。
- **只组装计划**：本模块不裁决级别（归 `policy.pdp`）、不执行（归 `policy.pep`）、不产事实
  （余额 / 卡片 / 产品一律读 `FactGateway`）。
- **锚点来源**：节点锚点只能是 `explicit`（用户本轮明说）；`derived` 不作节点锚点。
- **控制槽缺失即澄清**：口语只给 `span` 时抛 `ClarificationRequired`，**不猜金额 / 不猜卡片**；
  唯一例外是「从事实派生」的缺省值（如 L1 按余额挑储蓄账户、L5 继承原卡日限额）。
- **组合律共享同一锚点账本**：`AnchorLedger` 的 id 是 `a1, a2, …` 计数器，两段独立账本必然撞号，
  故 `build_life_event(..., ledger=...)` 支持外部注入。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta

from ai_banking_copilot.contracts.plan import (
    AnchorSource,
    IntentRef,
    Plan,
    PlanNode,
    validate_plan,
)
from ai_banking_copilot.gateway.core_bank import DEFAULT_BOOKING_DATE
from ai_banking_copilot.gateway.errors import GatewayNotFoundError
from ai_banking_copilot.gateway.facts import FactGateway
from ai_banking_copilot.gateway.models import Account, Card
from ai_banking_copilot.gateway.resolvers import resolve_payees
from ai_banking_copilot.gateway.seed import DEMO_USER_ID
from ai_banking_copilot.planning.anchors import AnchorLedger
from ai_banking_copilot.planning.llm import Intent
from ai_banking_copilot.scenarios.base import (
    ClarificationRequired,
    ScenarioHandler,
    ScenarioRefused,
    accept_data,
    as_date,
    as_int,
    as_money,
    as_str,
    grant_anchor,
    intent_text,
    require_trusted,
)
from ai_banking_copilot.scenarios.cross import (
    BIRTHDAY_BUDGET_CENTS,
    BIRTHDAY_MERCHANT,
    BIRTHDAY_ORDER_CENTS,
)
from ai_banking_copilot.scenarios.scheduling import REFERENCE_DATE


@dataclass(frozen=True)
class LifeEventPrototype:
    """一条生活链路原型（登记表条目，供前端面板与答辩材料直接引用）。"""

    event_id: str
    """原型编号：`L1` … `L7`。"""

    name: str
    """链路形态名（如「预算锁定型」）。"""

    triggers: tuple[str, ...]
    """口语触发词（**窄词**：不得抢既有域规则的常用说法）。"""

    domains: tuple[str, ...]
    """涉及的场景域（动作名前缀）。"""

    skeleton: tuple[str, ...]
    """DAG 骨架（白名单动作序列；实际节点数随参数增减）。"""

    reuse: str
    """复用的既有构建器 / 场景模块（避免另写一套）。"""

    description: str = ""
    """一句人话说明（前端面板直接展示，避免前端硬编码文案）。"""

    example: str = ""
    """示例话术（前端「用这句发起」按钮直接提交，走同一条 `/api/submit`）。"""


LIFE_EVENT_PROTOTYPES: tuple[LifeEventPrototype, ...] = (
    LifeEventPrototype(
        event_id="L1",
        name="预算锁定型",
        triggers=("生日", "寿宴", "纪念日", "过节"),
        domains=("funds", "merchant"),
        skeleton=("funds.freeze", "merchant.order", "funds.release"),
        reuse="scenarios.cross.birthday_plan",
        description="先把预算冻结住，商户下单后自动解冻剩余，超预算直接拒绝。",
        example="我妈下个月过生日",
    ),
    LifeEventPrototype(
        event_id="L2",
        name="定期出资型",
        triggers=("每月给", "按月给", "定期给", "固定出资", "每月生活费", "按月生活费"),
        domains=("transfer", "subscription"),
        skeleton=("transfer.schedule", "subscription.list"),
        reuse="scenarios.transfer + scenarios.subscriptions",
        description="按期自动转出，可在代扣清单里随时查看与停止。",
        example="每月给李娜 500 元生活费",
    ),
    LifeEventPrototype(
        event_id="L3",
        name="出行保障型",
        triggers=("出行", "出差", "旅行", "旅游", "境外"),
        domains=("card", "account"),
        skeleton=("card.set_limit", "account.transactions", "card.set_limit"),
        reuse="scenarios.cards",
        description="出行期间把日限额调低，到期自动恢复原额度。",
        example="下周三出差，把卡片日限额降到 1000 元",
    ),
    LifeEventPrototype(
        event_id="L4",
        name="大额凑集型",
        triggers=("凑钱", "首付", "学费", "装修款"),
        domains=("account", "wealth", "transfer"),
        skeleton=("account.balance", "wealth.redeem", "transfer.create"),
        reuse="scenarios.wealth + scenarios.transfer",
        description="先查余额，缺口才赎回，再一次性转出。",
        example="凑钱还房贷 500 元给李娜",
    ),
    LifeEventPrototype(
        event_id="L5",
        name="失卡止损型",
        triggers=("卡丢了", "银行卡丢", "卡片丢失", "手机丢了", "钱包被偷", "钱包丢了", "疑似盗刷"),
        domains=("card", "account"),
        skeleton=(
            "card.lock",
            "card.report_lost",
            "card.apply_virtual",
            "password.change",
            "account.transactions",
        ),
        reuse="scenarios.cards",
        description="锁卡 → 挂失 → 补虚拟卡 → 改密码 → 查近期交易，五步止损。",
        example="我的借记卡丢了",
    ),
    LifeEventPrototype(
        event_id="L6",
        name="阶段盘点型",
        triggers=("阶段盘点", "年度盘点", "年终总结", "全年盘点"),
        domains=("bills", "subscription"),
        skeleton=("bills.yearly", "subscription.list"),
        reuse="scenarios.bills + scenarios.subscriptions",
        description="年度账单 + 代扣盘点，纯查询不写资金。",
        example="阶段盘点",
    ),
    LifeEventPrototype(
        event_id="L7",
        name="分摊收款型",
        triggers=("分摊收款", "合租分摊", "团购收款", "份子钱", "AA制分摊"),
        domains=("transfer", "account", "bills"),
        skeleton=("transfer.request_collection", "account.transactions", "bills.monthly"),
        reuse="scenarios.transfer + scenarios.bills",
        description="发起分摊收款 → 确认到账 → 记到上月账单。",
        example="分摊收款，李娜 应付 100 元",
    ),
)
"""七条生活链路原型（顺序即触发优先级）。"""


@dataclass(frozen=True)
class LifeEventCombo:
    """两条原型的串接（组合律）：第一段末尾 → 第二段根节点。"""

    combo_id: str
    name: str
    first: str
    second: str
    triggers: tuple[str, ...]


LIFE_EVENT_COMBOS: tuple[LifeEventCombo, ...] = (
    LifeEventCombo(
        combo_id="L6->L4",
        name="盘点后凑钱",
        first="L6",
        second="L4",
        triggers=("盘点后凑钱", "年终盘点凑钱", "盘点凑钱"),
    ),
    LifeEventCombo(
        combo_id="L5->L3",
        name="失卡后出行",
        first="L5",
        second="L3",
        triggers=("补卡后出行", "丢卡后出行", "失卡后出差"),
    ),
    LifeEventCombo(
        combo_id="L1->L7",
        name="生日后分摊",
        first="L1",
        second="L7",
        triggers=("生日后分摊", "生日聚餐分摊", "生日分摊"),
    ),
)
"""三条组合律（第一段完成后，第二段接着办）。"""

_PROTOTYPE_BY_ID: Mapping[str, LifeEventPrototype] = {
    prototype.event_id: prototype for prototype in LIFE_EVENT_PROTOTYPES
}
_COMBO_BY_ID: Mapping[str, LifeEventCombo] = {combo.combo_id: combo for combo in LIFE_EVENT_COMBOS}

_PAYEE_ID_PREFIX = "p_"
_TRANSACTION_WINDOW_DAYS = 30
_DEFAULT_BUDGET_REASON = "生活事件预算冻结"

_ALIASES: Mapping[str, tuple[str, ...]] = {
    "account": ("account_id", "account"),
    "card": ("card_id", "card"),
    "payee": ("payee_id", "payee"),
    "payer": ("payer_id", "payer"),
    "product": ("product_id", "product"),
    "amount": ("amount_cents", "amount"),
    "budget": ("budget_cents", "budget"),
    "order_amount": ("order_cents", "order_amount"),
    "travel_limit": ("travel_limit", "travel_limit_cents", "daily_limit_cents", "daily_limit"),
    "virtual_limit": ("virtual_limit", "virtual_limit_cents", "virtual_card_limit"),
    "order_on": ("order_on", "when", "date"),
    "booked_on": ("booked_on", "when"),
    "merchant": ("merchant",),
    "occasion": ("occasion_id",),
    "reason": ("reason",),
    "start": ("start",),
    "end": ("end",),
    "year": ("year",),
    "month": ("month",),
    "note": ("note",),
}


def detect_life_event(text: str) -> str | None:
    """按 L1 → L7 顺序返回首个命中的原型编号；无命中返回 `None`（不猜）。"""
    for prototype in LIFE_EVENT_PROTOTYPES:
        if any(trigger in text for trigger in prototype.triggers):
            return prototype.event_id
    return None


def detect_life_combo(text: str) -> str | None:
    """返回首个命中的组合律编号；组合律**先于**单原型判定（否则只剩第一段）。"""
    for combo in LIFE_EVENT_COMBOS:
        if any(trigger in text for trigger in combo.triggers):
            return combo.combo_id
    return None


def build_life_event(
    event_id: str,
    params: Mapping[str, object],
    gateway: FactGateway,
    *,
    node_prefix: str = "n",
    ledger: AnchorLedger | None = None,
) -> Plan:
    """统一出口：原型编号 + 控制槽 → 带锚点的计划 DAG。

    `params` 取值可为原始值 / `Value` / `Slot`；控制槽走 `require_trusted`
    （显式 `unverified` 一律澄清），备注类文本走 `accept_data`。
    """
    builder = _BUILDERS.get(event_id)
    if builder is None:
        raise ScenarioRefused(f"未知生活事件原型：{event_id}")
    shared = ledger if ledger is not None else AnchorLedger()
    nodes = builder(params, gateway, shared, node_prefix)
    plan = Plan(plan_id=f"life_{event_id}", nodes=nodes)
    validate_plan(plan)
    return plan


def build_life_event_combo(
    combo_id: str, params: Mapping[str, object], gateway: FactGateway
) -> Plan:
    """组合律：两段**共享同一锚点账本**，第二段根节点依赖第一段末端节点。"""
    combo = _COMBO_BY_ID.get(combo_id)
    if combo is None:
        raise ScenarioRefused(f"未知组合律：{combo_id}")
    ledger = AnchorLedger()
    first = build_life_event(
        combo.first, _section(params, "first"), gateway, node_prefix="a", ledger=ledger
    )
    second = build_life_event(
        combo.second, _section(params, "second"), gateway, node_prefix="b", ledger=ledger
    )
    tail = first.nodes[-1].node_id
    merged = tuple(
        node if node.depends_on else replace(node, depends_on=(tail,)) for node in second.nodes
    )
    plan = Plan(plan_id=f"life_combo_{combo_id}", nodes=(*first.nodes, *merged))
    validate_plan(plan)
    return plan


class LifeEventsHandler(ScenarioHandler):
    """生活事件处理器（七条原型 + 三条组合律；排在 `cross` 之后）。"""

    @property
    def name(self) -> str:
        return "life_events"

    def matches(self, intents: Sequence[Intent]) -> bool:
        return any(_life_combo_of(intent) or _life_event_of(intent) for intent in intents)

    def build(self, intents: Sequence[Intent], gateway: FactGateway) -> Plan:
        for intent in intents:
            combo_id = _life_combo_of(intent)
            if combo_id is not None:
                return build_life_event_combo(combo_id, intent.slots, gateway)
        for intent in intents:
            event_id = _life_event_of(intent)
            if event_id is not None:
                return build_life_event(event_id, intent.slots, gateway)
        raise ScenarioRefused("未识别到生活事件原型")


def _life_event_of(intent: Intent) -> str | None:
    for key in ("life_event", "event"):
        raw = intent.slots.get(key)
        if raw is not None:
            value = accept_data(raw)
            if isinstance(value, str) and value in _PROTOTYPE_BY_ID:
                return value
    return detect_life_event(intent_text(intent))


def _life_combo_of(intent: Intent) -> str | None:
    for key in ("life_combo", "combo"):
        raw = intent.slots.get(key)
        if raw is not None:
            value = accept_data(raw)
            if isinstance(value, str) and value in _COMBO_BY_ID:
                return value
    return detect_life_combo(intent_text(intent))


def _section(params: Mapping[str, object], key: str) -> Mapping[str, object]:
    raw = params.get(key)
    if raw is None:
        return {}
    value = accept_data(raw)
    if not isinstance(value, Mapping):
        raise ClarificationRequired(key, f"{key} 参数必须是映射")
    return value


def _raw(params: Mapping[str, object], key: str) -> object | None:
    for candidate in _ALIASES.get(key, (key,)):
        if candidate in params:
            return params[candidate]
    return None


def _control(params: Mapping[str, object], key: str, name: str) -> object:
    raw = _raw(params, key)
    if raw is None:
        raise ClarificationRequired(name, f"缺少控制槽 {name}")
    return require_trusted(raw, name)


def _text(params: Mapping[str, object], key: str, name: str) -> str:
    return as_str(_control(params, key, name), name)


def _cents(params: Mapping[str, object], key: str, name: str) -> int:
    return as_money(_control(params, key, name), name).cents


def _opt_cents(params: Mapping[str, object], key: str) -> int | None:
    raw = _raw(params, key)
    if raw is None:
        return None
    return as_money(require_trusted(raw, key), key).cents


def _opt_int(params: Mapping[str, object], key: str) -> int | None:
    raw = _raw(params, key)
    if raw is None:
        return None
    return as_int(require_trusted(raw, key), key)


def _opt_day(params: Mapping[str, object], key: str) -> date | None:
    raw = _raw(params, key)
    if raw is None:
        return None
    return as_date(require_trusted(raw, key), key)


def _opt_text(params: Mapping[str, object], key: str) -> str | None:
    raw = _raw(params, key)
    if raw is None:
        return None
    value = accept_data(raw)
    return value if isinstance(value, str) and value else None


def _node(
    ledger: AnchorLedger,
    node_id: str,
    action: str,
    params: dict[str, object],
    *,
    detail: str,
    depends_on: tuple[str, ...] = (),
) -> PlanNode:
    return PlanNode(
        node_id=node_id,
        action=action,
        params=params,
        intent=IntentRef(
            source=AnchorSource.EXPLICIT,
            anchor_id=grant_anchor(ledger, AnchorSource.EXPLICIT, f"{detail}：{node_id}"),
        ),
        depends_on=depends_on,
    )


def _savings_account(gateway: FactGateway, needed: int = 0) -> Account | None:
    """从事实挑账户：储蓄优先，其次首个余额足够的账户；都没有则 `None`（不猜）。"""
    accounts = [record.payload for record in gateway.accounts_of(DEMO_USER_ID)]
    affordable = [item for item in accounts if item.balance.cents >= needed]
    savings = next((item for item in affordable if item.kind == "savings"), None)
    if savings is not None:
        return savings
    return affordable[0] if affordable else None


def _require_account(gateway: FactGateway, account_id: str) -> Account:
    try:
        return gateway.account(account_id).payload
    except GatewayNotFoundError as error:
        raise ScenarioRefused(f"账户不存在：{account_id}") from error


def _account_for_budget(
    params: Mapping[str, object], gateway: FactGateway, needed: int
) -> str:
    raw = _raw(params, "account")
    if raw is not None:
        account_id = as_str(require_trusted(raw, "account"), "account")
        _require_account(gateway, account_id)
        return account_id
    account = _savings_account(gateway, needed)
    if account is None:
        raise ClarificationRequired("account", "没有余额足够的账户，请指定冻结账户")
    return account.account_id


def _card_of(gateway: FactGateway, card_id: str) -> Card:
    try:
        return gateway.card(card_id).payload
    except GatewayNotFoundError as error:
        raise ScenarioRefused(f"卡片不存在：{card_id}") from error


def _require_product(gateway: FactGateway, product_id: str) -> None:
    try:
        gateway.wealth_product(product_id)
    except GatewayNotFoundError as error:
        raise ScenarioRefused(f"理财产品不存在：{product_id}") from error


def _resolve_party(
    params: Mapping[str, object], key: str, name: str, gateway: FactGateway
) -> str:
    """按名册确定性解析收款 / 付款方；多候选或无候选一律澄清（禁止自动择一）。"""
    label = "收款人" if key == "payee" else "付款方"
    raw = _raw(params, key)
    if raw is None:
        raise ClarificationRequired(name, f"请确认{label}（全名 / 账号尾号 / 手机尾号）")
    value = require_trusted(raw, name)
    if isinstance(value, str) and value.startswith(_PAYEE_ID_PREFIX):
        return value
    text = value if isinstance(value, str) else str(value)
    roster = tuple(record.payload for record in gateway.payees_of(DEMO_USER_ID))
    resolution = resolve_payees(roster, text)
    if resolution.needs_clarification:
        labels = tuple(
            f"{item.full_name}（尾号 {item.account_tail}）" for item in resolution.candidates
        )
        raise ClarificationRequired(name, f"{label}存在多个候选，请明确", labels)
    if not resolution.candidates:
        raise ClarificationRequired(name, f"未在名册中匹配到{label}，请确认")
    return resolution.candidates[0].payee_id


def _window(params: Mapping[str, object]) -> tuple[date, date]:
    end = _opt_day(params, "end")
    end_day = end if end is not None else REFERENCE_DATE
    start = _opt_day(params, "start")
    start_day = start if start is not None else end_day - timedelta(days=_TRANSACTION_WINDOW_DAYS)
    if start_day > end_day:
        raise ScenarioRefused("交易查询起始日不得晚于结束日")
    return start_day, end_day


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _billing_month(params: Mapping[str, object]) -> tuple[int, int]:
    year = _opt_int(params, "year")
    month = _opt_int(params, "month")
    if year is None or month is None:
        return _previous_month(REFERENCE_DATE.year, REFERENCE_DATE.month)
    if not 1 <= month <= 12:
        raise ScenarioRefused(f"记账月份不合法：{year}-{month}")
    return year, month


def _build_l1(
    params: Mapping[str, object], gateway: FactGateway, ledger: AnchorLedger, prefix: str
) -> tuple[PlanNode, ...]:
    """L1 预算锁定型：冻结预算 → 下单（从冻结额结算）→ 解冻剩余。"""
    budget = _opt_cents(params, "budget")
    budget_cents = BIRTHDAY_BUDGET_CENTS if budget is None else budget
    order = _opt_cents(params, "order_amount")
    order_cents = BIRTHDAY_ORDER_CENTS if order is None else order
    if budget_cents <= 0:
        raise ScenarioRefused("生活事件预算必须为正")
    if order_cents <= 0 or order_cents > budget_cents:
        raise ScenarioRefused("下单金额必须为正且不得超过冻结预算")
    account_id = _account_for_budget(params, gateway, budget_cents)
    order_on = _opt_day(params, "order_on")
    order_day = order_on if order_on is not None else REFERENCE_DATE
    merchant = _opt_text(params, "merchant") or BIRTHDAY_MERCHANT
    occasion_id = _opt_text(params, "occasion") or f"life:L1:{order_day.isoformat()}"
    reason = _opt_text(params, "reason") or _DEFAULT_BUDGET_REASON
    hold_id = f"life:L1:{account_id}:{order_day.isoformat()}"
    return (
        _node(
            ledger,
            f"{prefix}1",
            "funds.freeze",
            {
                "account_id": account_id,
                "amount_cents": budget_cents,
                "reason": reason,
                "budget_hold_id": hold_id,
            },
            detail="预算冻结",
        ),
        _node(
            ledger,
            f"{prefix}2",
            "merchant.order",
            {
                "merchant": merchant,
                "amount_cents": order_cents,
                "order_on": order_day.isoformat(),
                "occasion_id": occasion_id,
                "budget_hold_id": hold_id,
            },
            detail="下单结算",
            depends_on=(f"{prefix}1",),
        ),
        _node(
            ledger,
            f"{prefix}3",
            "funds.release",
            {
                "budget_hold_id": hold_id,
                "amount_cents": budget_cents - order_cents,
            },
            detail="解冻剩余",
            depends_on=(f"{prefix}1", f"{prefix}2"),
        ),
    )


def _build_l2(
    params: Mapping[str, object], gateway: FactGateway, ledger: AnchorLedger, prefix: str
) -> tuple[PlanNode, ...]:
    """L2 定期出资型：按期转出 → 代扣清单（可查可停）。"""
    payee_id = _resolve_party(params, "payee", "payee", gateway)
    amount = _cents(params, "amount", "amount")
    if amount <= 0:
        raise ScenarioRefused("定期出资金额必须为正")
    account = _savings_account(gateway)
    if account is None:
        raise ClarificationRequired("account", "未找到可用账户，请指定出资账户")
    booked_on = _opt_day(params, "booked_on")
    booked_day = booked_on if booked_on is not None else DEFAULT_BOOKING_DATE
    return (
        _node(
            ledger,
            f"{prefix}1",
            "transfer.schedule",
            {
                "account_id": account.account_id,
                "payee_id": payee_id,
                "amount_cents": amount,
                "currency": account.currency.strip().upper(),
                "booked_on": booked_day.isoformat(),
            },
            detail="定期出资",
        ),
        _node(
            ledger,
            f"{prefix}2",
            "subscription.list",
            {"user_id": DEMO_USER_ID},
            detail="代扣清单",
            depends_on=(f"{prefix}1",),
        ),
    )


def _build_l3(
    params: Mapping[str, object], gateway: FactGateway, ledger: AnchorLedger, prefix: str
) -> tuple[PlanNode, ...]:
    """L3 出行保障型：临时收紧额度 → 期间消费核查 → 到日恢复原限额。"""
    card_id = _text(params, "card", "card")
    card = _card_of(gateway, card_id)
    original = card.daily_limit.cents
    travel_limit = _cents(params, "travel_limit", "travel_limit")
    if travel_limit <= 0:
        raise ScenarioRefused("出行限额必须为正")
    if travel_limit >= original:
        raise ScenarioRefused("出行限额必须低于原日限额（否则不构成收紧）")
    nodes = [
        _node(
            ledger,
            f"{prefix}1",
            "card.set_limit",
            {"card_id": card_id, "daily_limit_cents": travel_limit},
            detail="出行收紧额度",
        )
    ]
    account = _savings_account(gateway)
    if account is not None:
        start, end = _window(params)
        nodes.append(
            _node(
                ledger,
                f"{prefix}2",
                "account.transactions",
                {
                    "account_id": account.account_id,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                detail="出行期间交易",
                depends_on=(f"{prefix}1",),
            )
        )
    nodes.append(
        _node(
            ledger,
            f"{prefix}{len(nodes) + 1}",
            "card.set_limit",
            {"card_id": card_id, "daily_limit_cents": original},
            detail="恢复原日限额",
            depends_on=tuple(node.node_id for node in nodes),
        )
    )
    return tuple(nodes)


def _build_l4(
    params: Mapping[str, object], gateway: FactGateway, ledger: AnchorLedger, prefix: str
) -> tuple[PlanNode, ...]:
    """L4 大额凑集型：查余额 → 赎回补缺口（缺口为 0 则省略）→ 一次性转出。"""
    amount = _cents(params, "amount", "amount")
    if amount <= 0:
        raise ScenarioRefused("凑集金额必须为正")
    account = _savings_account(gateway)
    if account is None:
        raise ClarificationRequired("account", "未找到可用账户，请指定转出账户")
    payee_id = _resolve_party(params, "payee", "payee", gateway)
    product_id = _text(params, "product", "product")
    _require_product(gateway, product_id)
    booked_on = _opt_day(params, "booked_on")
    booked_day = booked_on if booked_on is not None else DEFAULT_BOOKING_DATE
    gap = max(0, amount - account.balance.cents)
    nodes = [
        _node(
            ledger,
            f"{prefix}1",
            "account.balance",
            {"account_id": account.account_id},
            detail="余额盘点",
        )
    ]
    depends_on = (f"{prefix}1",)
    index = 2
    if gap > 0:
        redeem_id = f"{prefix}{index}"
        nodes.append(
            _node(
                ledger,
                redeem_id,
                "wealth.redeem",
                {
                    "account_id": account.account_id,
                    "product_id": product_id,
                    "amount_cents": gap,
                },
                detail="赎回补缺口",
                depends_on=depends_on,
            )
        )
        depends_on = (redeem_id,)
        index += 1
    nodes.append(
        _node(
            ledger,
            f"{prefix}{index}",
            "transfer.create",
            {
                "account_id": account.account_id,
                "payee_id": payee_id,
                "amount_cents": amount,
                "currency": account.currency.strip().upper(),
                "booked_on": booked_day.isoformat(),
            },
            detail="一次性转出",
            depends_on=depends_on,
        )
    )
    return tuple(nodes)


def _build_l5(
    params: Mapping[str, object], gateway: FactGateway, ledger: AnchorLedger, prefix: str
) -> tuple[PlanNode, ...]:
    """L5 失卡止损型：锁卡 → 挂失 → 补虚拟卡 → 改密码 → 近期交易核查。"""
    card_id = _text(params, "card", "card")
    card = _card_of(gateway, card_id)
    virtual_limit = _opt_cents(params, "virtual_limit")
    limit_cents = card.daily_limit.cents if virtual_limit is None else virtual_limit
    if limit_cents <= 0:
        raise ScenarioRefused("虚拟卡日限额必须为正")
    nodes = [
        _node(
            ledger,
            f"{prefix}1",
            "card.lock",
            {"card_id": card_id},
            detail="先行锁卡",
        ),
        _node(
            ledger,
            f"{prefix}2",
            "card.report_lost",
            {"card_id": card_id},
            detail="挂失",
            depends_on=(f"{prefix}1",),
        ),
        _node(
            ledger,
            f"{prefix}3",
            "card.apply_virtual",
            {"user_id": DEMO_USER_ID, "daily_limit_cents": limit_cents},
            detail="补虚拟卡",
            depends_on=(f"{prefix}2",),
        ),
        _node(
            ledger,
            f"{prefix}4",
            "password.change",
            {"user_id": DEMO_USER_ID},
            detail="改交易密码",
            depends_on=(f"{prefix}3",),
        ),
    ]
    account = _savings_account(gateway)
    if account is not None:
        start, end = _window(params)
        nodes.append(
            _node(
                ledger,
                f"{prefix}5",
                "account.transactions",
                {
                    "account_id": account.account_id,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                detail="近期交易核查",
                depends_on=(f"{prefix}4",),
            )
        )
    return tuple(nodes)


def _build_l6(
    params: Mapping[str, object], gateway: FactGateway, ledger: AnchorLedger, prefix: str
) -> tuple[PlanNode, ...]:
    """L6 阶段盘点型：年度账单 → 代扣盘点（**纯查询**，可落绿色）。"""
    year = _opt_int(params, "year")
    year_value = REFERENCE_DATE.year if year is None else year
    if not 1970 <= year_value <= 9999:
        raise ScenarioRefused(f"盘点年份不合法：{year_value}")
    return (
        _node(
            ledger,
            f"{prefix}1",
            "bills.yearly",
            {"user_id": DEMO_USER_ID, "year": year_value},
            detail="年度盘点",
        ),
        _node(
            ledger,
            f"{prefix}2",
            "subscription.list",
            {"user_id": DEMO_USER_ID},
            detail="代扣盘点",
            depends_on=(f"{prefix}1",),
        ),
    )


def _build_l7(
    params: Mapping[str, object], gateway: FactGateway, ledger: AnchorLedger, prefix: str
) -> tuple[PlanNode, ...]:
    """L7 分摊收款型：发起收款 → 确认到账 → 记账（上月账单）。"""
    payer_id = _resolve_party(params, "payer", "payer", gateway)
    amount = _cents(params, "amount", "amount")
    if amount <= 0:
        raise ScenarioRefused("分摊金额必须为正")
    year, month = _billing_month(params)
    nodes = [
        _node(
            ledger,
            f"{prefix}1",
            "transfer.request_collection",
            {"payer_id": payer_id, "amount_cents": amount},
            detail="发起分摊收款",
        )
    ]
    account = _savings_account(gateway)
    if account is not None:
        start, end = _window(params)
        nodes.append(
            _node(
                ledger,
                f"{prefix}2",
                "account.transactions",
                {
                    "account_id": account.account_id,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
                detail="确认到账",
                depends_on=(f"{prefix}1",),
            )
        )
    nodes.append(
        _node(
            ledger,
            f"{prefix}{len(nodes) + 1}",
            "bills.monthly",
            {"user_id": DEMO_USER_ID, "year": year, "month": month},
            detail="记账",
            depends_on=tuple(node.node_id for node in nodes),
        )
    )
    return tuple(nodes)


_BUILDERS: Mapping[
    str, Callable[[Mapping[str, object], FactGateway, AnchorLedger, str], tuple[PlanNode, ...]]
] = {
    "L1": _build_l1,
    "L2": _build_l2,
    "L3": _build_l3,
    "L4": _build_l4,
    "L5": _build_l5,
    "L6": _build_l6,
    "L7": _build_l7,
}
"""原型编号 → 构建器（新增原型只需在此登记 + 在上表加一行）。"""
