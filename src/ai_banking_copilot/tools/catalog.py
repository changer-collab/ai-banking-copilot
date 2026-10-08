"""六场景动作目录：把 `README.md` 的「规范动作表（唯一真源）」20 个动作固化为**白名单契约**。

覆盖（PRD §4.1 / §5.2）：
- ① 智能转账：`transfer.create` / `transfer.schedule`（`remark` 为数据槽，演示 H3）
- ② 账单分析：`account.balance` / `account.transactions` / `bills.monthly` /
  `bills.category_trend` / `bills.yearly`
- ③ 理财操作：`wealth.list`（纯查询）/ `wealth.subscribe` / `wealth.redeem`
- ④ 卡片管理：`card.list`（纯查询）/ `card.lock` / `card.unlock` / `card.report_lost` /
  `card.apply_virtual` / `card.set_limit`
- ⑤ 订阅代扣：`subscription.list`（纯查询）/ `subscription.cancel` / `subscription.sign`
- ⑥ 跨场景联动：`funds.freeze` / `funds.release`

**动作名唯一真源**：本目录就是规范动作表的**唯一真源**（旧文档指向的 README 表已不存在）。
基线 20 个动作**不得增删或改名**；新增动作按 `docs/待修复清单.md` 第七节「硬性协作规则」一次改齐
（本目录自己域那一段 + 自己场景模块的 handler + 评测用例）；计数断言只设**下限**（防误删）。
`bills.yearly` 即步骤 2 冻结的第一个新增动作。

**与 01 写原语的关系**：`transfer.create` / `wealth.subscribe` / `wealth.redeem` /
`subscription.cancel` / `subscription.sign` / `funds.freeze` / `funds.release` /
`card.lock` / `card.unlock` / `card.report_lost` / `card.apply_virtual` / `card.set_limit`
均有 01 的 `CoreBank` 写原语（其中 `subscription.sign` 对应新增的 `sign_subscription`）；
`transfer.schedule` 复用转账语义（带 `booked_on`）。**执行处理器由各场景模块自带、组合根汇入
03 的 PEP handler 注册表**（本层只做声明，不执行）。

**写动作的 `user_id` 控制槽**（README 裁决 #16）：`transfer.create` / `transfer.schedule` /
`wealth.subscribe` / `wealth.redeem` / `subscription.sign` 一律声明**必填控制槽** `user_id`
（`SlotKind.CONTROL`、`str`），供 03 的 PEP 写分派取用户身份；会话层与评测链装配 `params`
时填入 `DEMO_USER_ID`。

**级别归属**：本目录只声明 `RiskClass` 与 `is_pure_query`，**不产出三色级别**（那是 03）。
官方把「订阅查询」与「订阅取消」并列在黄色区间；本层如实标记 `subscription.list` 为
纯查询，是否按官方口径升档由 03 决定。
"""

from __future__ import annotations

from datetime import date

from ai_banking_copilot.contracts.taint import SlotKind
from ai_banking_copilot.tools.registry import (
    ActionKind,
    ParamSpec,
    RiskClass,
    ToolRegistry,
    ToolSpec,
)

_TRANSFER_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        action="transfer.create",
        risk=RiskClass.LARGE_FUNDS,
        kind=ActionKind.OUTFLOW,
        params=(
            ParamSpec("user_id", SlotKind.CONTROL, str, slot_name="user_id"),
            ParamSpec("account_id", SlotKind.CONTROL, str, slot_name="account"),
            ParamSpec("payee_id", SlotKind.CONTROL, str, slot_name="payee"),
            ParamSpec("amount_cents", SlotKind.CONTROL, int, slot_name="amount"),
            ParamSpec("booked_on", SlotKind.CONTROL, date, slot_name="when"),
            ParamSpec("remark", SlotKind.DATA, str, required=False),
        ),
        is_pure_query=False,
        reversible=True,
        money_param="amount_cents",
        description="向名册收款人转出资金（备注只入数据槽，永不被解释为指令）",
    ),
    ToolSpec(
        action="transfer.schedule",
        risk=RiskClass.LARGE_FUNDS,
        kind=ActionKind.OUTFLOW,
        params=(
            ParamSpec("user_id", SlotKind.CONTROL, str, slot_name="user_id"),
            ParamSpec("account_id", SlotKind.CONTROL, str, slot_name="account"),
            ParamSpec("payee_id", SlotKind.CONTROL, str, slot_name="payee"),
            ParamSpec("amount_cents", SlotKind.CONTROL, int, slot_name="amount"),
            ParamSpec("booked_on", SlotKind.CONTROL, date, slot_name="when"),
            ParamSpec("remark", SlotKind.DATA, str, required=False),
        ),
        is_pure_query=False,
        reversible=True,
        money_param="amount_cents",
        description="定时转账（未来流出；执行日由 booked_on 指定）",
    ),
)

_BILLS_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        action="account.balance",
        risk=RiskClass.READ_ONLY,
        kind=ActionKind.QUERY,
        params=(ParamSpec("account_id", SlotKind.CONTROL, str, slot_name="account"),),
        is_pure_query=True,
        reversible=True,
        money_param=None,
        description="查询账户余额（纯查询，可落入官方绿色）",
    ),
    ToolSpec(
        action="account.transactions",
        risk=RiskClass.READ_ONLY,
        kind=ActionKind.QUERY,
        params=(
            ParamSpec("account_id", SlotKind.CONTROL, str, slot_name="account"),
            ParamSpec("start", SlotKind.CONTROL, date),
            ParamSpec("end", SlotKind.CONTROL, date),
        ),
        is_pure_query=True,
        reversible=True,
        money_param=None,
        description="按日期区间查询交易记录（纯查询，数值来自事实网关）",
    ),
    ToolSpec(
        action="bills.monthly",
        risk=RiskClass.READ_ONLY,
        kind=ActionKind.QUERY,
        params=(
            ParamSpec("user_id", SlotKind.CONTROL, str, slot_name="user_id"),
            ParamSpec("year", SlotKind.CONTROL, int),
            ParamSpec("month", SlotKind.CONTROL, int),
        ),
        is_pure_query=True,
        reversible=True,
        money_param=None,
        description="月度账单分类汇总（纯查询）",
    ),
    ToolSpec(
        action="bills.category_trend",
        risk=RiskClass.READ_ONLY,
        kind=ActionKind.QUERY,
        params=(
            ParamSpec("user_id", SlotKind.CONTROL, str, slot_name="user_id"),
            ParamSpec("category", SlotKind.CONTROL, str),
            ParamSpec("base_year", SlotKind.CONTROL, int),
            ParamSpec("base_month", SlotKind.CONTROL, int),
        ),
        is_pure_query=True,
        reversible=True,
        money_param=None,
        description="分类同比 / 环比（派生数字走算子白名单，纯查询）",
    ),
    ToolSpec(
        action="bills.yearly",
        risk=RiskClass.READ_ONLY,
        kind=ActionKind.QUERY,
        params=(
            ParamSpec("user_id", SlotKind.CONTROL, str, slot_name="user_id"),
            ParamSpec("year", SlotKind.CONTROL, int),
        ),
        is_pure_query=True,
        reversible=True,
        money_param=None,
        description="年度账单汇总（12 个月度账单事实的年度合计，纯查询）",
    ),
)

_WEALTH_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        action="wealth.list",
        risk=RiskClass.READ_ONLY,
        kind=ActionKind.QUERY,
        params=(),
        is_pure_query=True,
        reversible=True,
        money_param=None,
        description="理财产品与事实指标列表（纯查询，只陈述事实、不承诺收益）",
    ),
    ToolSpec(
        action="wealth.subscribe",
        risk=RiskClass.LARGE_FUNDS,
        kind=ActionKind.OUTFLOW,
        params=(
            ParamSpec("user_id", SlotKind.CONTROL, str, slot_name="user_id"),
            ParamSpec("account_id", SlotKind.CONTROL, str, slot_name="account"),
            ParamSpec("product_id", SlotKind.CONTROL, str, slot_name="product"),
            ParamSpec("amount_cents", SlotKind.CONTROL, int, slot_name="amount"),
        ),
        is_pure_query=False,
        reversible=True,
        money_param="amount_cents",
        description="理财申购（官方红色典型；风险测评由场景层先行把关）",
    ),
    ToolSpec(
        action="wealth.redeem",
        risk=RiskClass.LARGE_FUNDS,
        kind=ActionKind.INFLOW,
        params=(
            ParamSpec("user_id", SlotKind.CONTROL, str, slot_name="user_id"),
            ParamSpec("account_id", SlotKind.CONTROL, str, slot_name="account"),
            ParamSpec("product_id", SlotKind.CONTROL, str, slot_name="product"),
            ParamSpec("amount_cents", SlotKind.CONTROL, int, slot_name="amount"),
        ),
        is_pure_query=False,
        reversible=True,
        money_param="amount_cents",
        description="理财赎回（资金回流入账）",
    ),
)

_CARDS_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        action="card.list",
        risk=RiskClass.READ_ONLY,
        kind=ActionKind.QUERY,
        params=(ParamSpec("user_id", SlotKind.CONTROL, str, slot_name="user_id"),),
        is_pure_query=True,
        reversible=True,
        money_param=None,
        description="卡片与额度列表（纯查询）",
    ),
    ToolSpec(
        action="card.lock",
        risk=RiskClass.SMALL_CHANGE,
        kind=ActionKind.STATE,
        params=(
            ParamSpec("card_id", SlotKind.CONTROL, str, slot_name="card"),
            ParamSpec("note", SlotKind.DATA, str, required=False),
        ),
        is_pure_query=False,
        reversible=True,
        money_param=None,
        description="限制卡片交易（可逆状态变更）",
    ),
    ToolSpec(
        action="card.unlock",
        risk=RiskClass.SMALL_CHANGE,
        kind=ActionKind.STATE,
        params=(ParamSpec("card_id", SlotKind.CONTROL, str, slot_name="card"),),
        is_pure_query=False,
        reversible=True,
        money_param=None,
        description="解除卡片限制（可逆状态变更）",
    ),
    ToolSpec(
        action="card.report_lost",
        risk=RiskClass.IRREVERSIBLE,
        kind=ActionKind.IRREVERSIBLE,
        params=(
            ParamSpec("card_id", SlotKind.CONTROL, str, slot_name="card"),
            ParamSpec("reason", SlotKind.DATA, str, required=False),
        ),
        is_pure_query=False,
        reversible=False,
        money_param=None,
        description="挂失卡片（不可逆：只能加严，无回滚）",
    ),
    ToolSpec(
        action="card.apply_virtual",
        risk=RiskClass.SMALL_CHANGE,
        kind=ActionKind.STATE,
        params=(
            ParamSpec("user_id", SlotKind.CONTROL, str, slot_name="user_id"),
            ParamSpec("daily_limit_cents", SlotKind.CONTROL, int, slot_name="daily_limit"),
        ),
        is_pure_query=False,
        reversible=True,
        money_param="daily_limit_cents",
        description="申请虚拟卡（官方黄色；额度进控制槽）",
    ),
    ToolSpec(
        action="card.set_limit",
        risk=RiskClass.SMALL_CHANGE,
        kind=ActionKind.STATE,
        params=(
            ParamSpec("card_id", SlotKind.CONTROL, str, slot_name="card"),
            ParamSpec("daily_limit_cents", SlotKind.CONTROL, int, slot_name="daily_limit"),
        ),
        is_pure_query=False,
        reversible=True,
        money_param="daily_limit_cents",
        description="调整卡片额度（官方黄色；额度进控制槽）",
    ),
)

_SUBSCRIPTION_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        action="subscription.list",
        risk=RiskClass.READ_ONLY,
        kind=ActionKind.QUERY,
        params=(ParamSpec("user_id", SlotKind.CONTROL, str, slot_name="user_id"),),
        is_pure_query=True,
        reversible=True,
        money_param=None,
        description="订阅与代扣列表（纯查询）",
    ),
    ToolSpec(
        action="subscription.cancel",
        risk=RiskClass.SMALL_CHANGE,
        kind=ActionKind.CANCEL_OUTFLOW,
        params=(ParamSpec("subscription_id", SlotKind.CONTROL, str, slot_name="subscription"),),
        is_pure_query=False,
        reversible=True,
        money_param=None,
        description="取消订阅（官方黄色；未来流出转为负）",
    ),
    ToolSpec(
        action="subscription.sign",
        risk=RiskClass.SMALL_CHANGE,
        kind=ActionKind.OUTFLOW,
        params=(
            ParamSpec("user_id", SlotKind.CONTROL, str, slot_name="user_id"),
            ParamSpec("merchant", SlotKind.CONTROL, str, slot_name="merchant"),
            ParamSpec("amount_cents", SlotKind.CONTROL, int, slot_name="amount_cents"),
            ParamSpec("period", SlotKind.CONTROL, str, slot_name="period"),
            ParamSpec("next_charge_on", SlotKind.CONTROL, date, slot_name="next_charge_on"),
        ),
        is_pure_query=False,
        reversible=True,
        money_param="amount_cents",
        description="代扣签约（未来周期性流出；period 为 monthly / yearly）",
    ),
)

_CROSS_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        action="funds.freeze",
        risk=RiskClass.SMALL_CHANGE,
        kind=ActionKind.OUTFLOW,
        params=(
            ParamSpec("account_id", SlotKind.CONTROL, str, slot_name="account"),
            ParamSpec("amount_cents", SlotKind.CONTROL, int, slot_name="amount"),
            ParamSpec("reason", SlotKind.DATA, str, required=False),
        ),
        is_pure_query=False,
        reversible=True,
        money_param="amount_cents",
        description="冻结指定金额（跨场景预算锁定；cf 口径视同流出）",
    ),
    ToolSpec(
        action="funds.release",
        risk=RiskClass.SMALL_CHANGE,
        kind=ActionKind.INFLOW,
        params=(ParamSpec("hold_id", SlotKind.CONTROL, str, slot_name="hold"),),
        is_pure_query=False,
        reversible=True,
        money_param=None,
        description="解冻已冻结资金（跨场景预算释放）",
    ),
)

_MERCHANT_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        action="merchant.order",
        risk=RiskClass.LARGE_FUNDS,
        kind=ActionKind.OUTFLOW,
        params=(
            ParamSpec("user_id", SlotKind.CONTROL, str, slot_name="user_id"),
            ParamSpec("merchant", SlotKind.CONTROL, str, slot_name="merchant"),
            ParamSpec("amount_cents", SlotKind.CONTROL, int, slot_name="amount"),
            # 规格 §2.2 的 `slot_name=when` 与 transfer 的 `when → booked_on` 在**由目录派生的**
            # `planning.slots.SLOT_PARAMS` 里冲突（同名槽位不得映射到不同参数名，构造期即抛）。
            # 故这里声明为 `slot_name=None`：订单日期由**场景层**（`scenarios.cross.birthday_plan`）
            # 填充，规划层不补全——与 `ParamSpec.slot_name` 的判据（「是否由规划层补全」）一致。
            ParamSpec("order_on", SlotKind.CONTROL, date),
        ),
        is_pure_query=False,
        reversible=False,
        money_param="amount_cents",
        description="向商户下单（生日联动：生日前 2 天下单；不可逆流出）",
    ),
)

_SCENARIO_SPECS: tuple[ToolSpec, ...] = (
    _TRANSFER_SPECS
    + _BILLS_SPECS
    + _WEALTH_SPECS
    + _CARDS_SPECS
    + _SUBSCRIPTION_SPECS
    + _CROSS_SPECS
    + _MERCHANT_SPECS
)


def build_default_registry() -> ToolRegistry:
    """构建动作白名单（确定性、无参、无副作用）。"""
    registry = ToolRegistry()
    for spec in _SCENARIO_SPECS:
        registry.register(spec)
    return registry
