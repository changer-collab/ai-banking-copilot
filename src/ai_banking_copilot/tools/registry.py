"""工具白名单注册表与动作契约（纯类型与纯函数，禁止 I/O）。

职责边界（**本层不越界**）：
- 只**声明与校验**：动作名、参数槽位（`SlotKind`）、参数类型、风险等级（`RiskClass`）、
  是否纯查询（`is_pure_query`）、是否可逆（`reversible`）、金额参数名（`money_param`）。
- **不做三色映射**：`RiskClass` 是动作的**内在风险**，映射到官方三色（绿 / 黄 / 红）
  是 03（`policy.pdp`）的职责，本层不产出授权级别。
- **不执行、不落盘**：不 import `sqlite3` / `requests` / `httpx`，不打开文件、不读文件。

H3「输入零信任」在本层的落点：控制槽（`SlotKind.CONTROL`）接受**裸值**（程序化可信）与
`Value(TRUSTED)` / `Slot(CONTROL, Value(TRUSTED))`；**只拒绝 `Value(UNVERIFIED)`**，
一律以 `ForbiddenParamError` 拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ai_banking_copilot.contracts.taint import Slot, SlotKind, Trust, Value


class RiskClass(StrEnum):
    """动作的**内在**风险等级（与授权级别解耦；三色映射见 03）。"""

    READ_ONLY = "read_only"
    """纯查询：唯一允许落入官方绿色的一类。"""

    NOTIFY = "notify"
    """通知 / 提醒类：不改资金与银行状态，但产生对外动作。"""

    SMALL_CHANGE = "small_change"
    """小额或可逆状态变更（订阅取消、卡片限制 / 解锁）。"""

    LARGE_FUNDS = "large_funds"
    """资金变动动作（转账、理财申赎）；额度档由 PDP 按金额细化。"""

    IRREVERSIBLE = "irreversible"
    """不可逆动作（挂失）：只能加严，无回滚。"""


class ActionKind(StrEnum):
    """动作的**净效应类别**（cf 口径，PRD §5.3）。

    **2026-10-07 从 `planning.llm` 下移到本层**：类别是动作契约的一部分，且规划层依赖执行层
    （`docs/architecture.md` §6.1「依赖 / 调用方向自上而下、单向」），放本层才不违反依赖方向。
    `planning.llm` 仍 re-export 同名枚举，避免调用方与测试被同时牵动。
    """

    QUERY = "query"
    """纯查询：不进 cf。"""

    STATE = "state"
    """状态变更但非不可逆、无资金流：不进 cf。"""

    OUTFLOW = "outflow"
    """流出（正 cf）：转账 / 定时转账 / 理财申购 / 代扣签约 / 自动续费 / 冻结资金。"""

    INFLOW = "inflow"
    """流入（负流出）：理财赎回 / 解冻。"""

    CANCEL_OUTFLOW = "cancel_outflow"
    """取消未来流出（记负）：订阅取消 / 解约。"""

    IRREVERSIBLE = "irreversible"
    """不可逆：挂失——**不入 cf**，单列触发 ≥ 红色 + L3。"""


@dataclass(frozen=True)
class ParamSpec:
    """一个动作参数的声明：名字 + 槽位类别 + 期望 Python 类型 + 是否必填。

    槽位类别决定 taint 规则：`CONTROL` 只接受可信来源，`DATA` 可承载 `UNVERIFIED`。
    """

    name: str
    slot: SlotKind
    py_type: type[object]
    required: bool = True
    slot_name: str | None = None
    """**槽位名**——非空表示该参数**由规划层补全**，值为规划层用的槽位名（步骤 1.1）。

    两套词汇**必须分开**：参数名是执行层调用用的字面名，槽位名是规划层补全用的名字。
    实测差异：`account_id`/`account`、`card_id`/`card`、`subscription_id`/`subscription`、
    `hold_id`/`hold`、`payee_id`/`payee`、`amount_cents`/`amount`（`transfer.create`）。

    **判据是「是否由规划层补全」，不是「是否与参数名同名」。** 有些参数虽为必填，却由**场景层**
    填充、规划层不管，故必须留 `None`：`bills.monthly` 的 `year` / `month`、
    `account.transactions` 的 `start` / `end`、`bills.category_trend` 的 `category` /
    `base_year` / `base_month`、以及各动作的 `note` / `reason` 等数据槽。

    同名但确由规划层补全的（`user_id`、`merchant`、`period`、`next_charge_on`）**要显式填同名值**，
    否则会被判为非槽位参数。`REQUIRED_SLOTS` 据此派生，见步骤 1.3。
    """

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ParamSpec.name 不能为空")
        if not isinstance(self.slot, SlotKind):
            raise TypeError(f"slot 必须是 SlotKind 成员，收到 {type(self.slot).__name__}")
        if not isinstance(self.py_type, type):
            raise TypeError(f"py_type 必须是类型对象，收到 {self.py_type!r}")
        if self.slot_name is not None and not self.slot_name:
            raise ValueError("ParamSpec.slot_name 不能为空串（不同名时必填，同名请留 None）")


@dataclass(frozen=True)
class ToolSpec:
    """一个白名单动作的契约。

    三条自洽不变式（构造即校验，防止目录写错）：
    1. `is_pure_query=True ⟺ risk is READ_ONLY`（绿色只含纯查询）；
    2. `risk is IRREVERSIBLE ⇒ reversible is False`；
    3. `money_param` 必须指向一个已声明的 `CONTROL` 槽参数（金额只能进控制槽）。
    """

    action: str
    risk: RiskClass
    kind: ActionKind | None = None
    """**净效应类别**（cf 口径）——2026-10-07 新增（`docs/待修复清单.md` 步骤 1.1）。

    类别**不能由 `risk` 推出**：`wealth.subscribe`(OUTFLOW) 与 `wealth.redeem`(INFLOW)
    同为 `LARGE_FUNDS`；`subscription.cancel`(CANCEL_OUTFLOW) 与 `card.lock`(STATE)
    同为 `SMALL_CHANGE`，故须逐动作声明。

    **可空只为便利测试构造临时 `ToolSpec`**（测试替身不该被强制声明类别）；02 规范目录的
    20 项必须逐一声明——该 totality 由 `planning.llm.ACTION_KINDS` 的派生处把关（缺失即拒绝）。
    """
    params: tuple[ParamSpec, ...] = ()
    is_pure_query: bool = False
    reversible: bool = True
    money_param: str | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not self.action:
            raise ValueError("ToolSpec.action 不能为空")
        if not isinstance(self.risk, RiskClass):
            raise TypeError(f"risk 必须是 RiskClass 成员，收到 {type(self.risk).__name__}")
        if self.kind is not None and not isinstance(self.kind, ActionKind):
            raise TypeError(f"kind 必须是 ActionKind 成员，收到 {type(self.kind).__name__}")
        if self.kind is not None and (self.kind is ActionKind.QUERY) is not self.is_pure_query:
            raise ValueError(f"{self.action}：kind=QUERY ⟺ is_pure_query（绿色只含纯查询）")
        if not self.description:
            raise ValueError("ToolSpec.description 不能为空（禁止占位）")
        names = [param.name for param in self.params]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.action} 存在重复参数名")
        if self.is_pure_query and self.risk is not RiskClass.READ_ONLY:
            raise ValueError("纯查询动作的 risk 必须是 READ_ONLY（绿色只含纯查询）")
        if self.risk is RiskClass.READ_ONLY and not self.is_pure_query:
            raise ValueError("risk=READ_ONLY 的动作必须是纯查询（is_pure_query=True）")
        if self.risk is RiskClass.IRREVERSIBLE and self.reversible:
            raise ValueError("不可逆动作的 reversible 必须为 False")
        if self.money_param is not None:
            declared = {param.name: param for param in self.params}
            if self.money_param not in declared:
                raise ValueError(f"money_param={self.money_param} 未在 params 中声明")
            if declared[self.money_param].slot is not SlotKind.CONTROL:
                raise ValueError("金额参数必须声明为控制槽（CONTROL）")


class UnknownActionError(KeyError):
    """动作不在白名单内。"""


class ParamValidationError(ValueError):
    """参数形状 / 必填 / 类型校验失败。"""


class ForbiddenParamError(ValueError):
    """控制槽参数携带不可信来源标记（H3 输入零信任）。"""


@dataclass(frozen=True)
class ToolCall:
    """一次待执行的工具调用：动作名 + 参数表。

    参数值允许三种形态（由 `validate_params` 归一）：裸值（程序化可信，控制槽亦接受）、
    `Value`（带来源标记）、`Slot`（带槽位类别 + 来源标记）。
    """

    action: str
    params: dict[str, object] = field(default_factory=dict)


class ToolRegistry:
    """动作白名单注册表。默认空表，由 `catalog.build_default_registry()` 填充。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """登记动作契约；重复登记同一动作名直接拒绝（白名单封闭）。"""
        if spec.action in self._specs:
            raise ValueError(f"动作已登记：{spec.action}")
        self._specs[spec.action] = spec

    def get(self, action: str) -> ToolSpec:
        """按动作名取契约；不存在抛 `UnknownActionError`。"""
        spec = self._specs.get(action)
        if spec is None:
            raise UnknownActionError(f"动作不在白名单内：{action}")
        return spec

    def actions(self) -> tuple[str, ...]:
        """全部已登记动作名（字典序），供规划内核枚举候选动作。"""
        return tuple(sorted(self._specs))

    def validate_params(self, call: ToolCall) -> dict[str, object]:
        """强校验一次调用并返回**去标记后的原始值**，供 PEP 直接执行。

        校验顺序：动作存在 → 无未声明参数 → 必填齐备 → 槽位 taint → 类型。
        """
        spec = self.get(call.action)
        declared = {param.name: param for param in spec.params}
        for name in call.params:
            if name not in declared:
                raise ParamValidationError(f"{spec.action} 未声明参数：{name}")
        resolved: dict[str, object] = {}
        for param in spec.params:
            if param.name not in call.params:
                if param.required:
                    raise ParamValidationError(f"{spec.action} 缺少必填参数：{param.name}")
                continue
            raw = _resolve_slot_value(param, call.params[param.name])
            if raw is not None and not isinstance(raw, param.py_type):
                raise ParamValidationError(
                    f"{spec.action}.{param.name} 期望 {param.py_type.__name__}，"
                    f"收到 {type(raw).__name__}"
                )
            resolved[param.name] = raw
        return resolved


def _resolve_slot_value(param: ParamSpec, value: object) -> object:
    """按槽位类别解出原始值：控制槽**只拒绝 unverified**，裸值视为程序化可信（H3）。"""
    if isinstance(value, Slot):
        if value.kind is not param.slot:
            raise ParamValidationError(
                f"{param.name} 声明的槽位是 {param.slot.value}，收到 {value.kind.value}"
            )
        return _unwrap(param, value.value)
    if isinstance(value, Value):
        return _unwrap(param, value)
    return value


def _unwrap(param: ParamSpec, value: Value[object]) -> object:
    """控制槽只拒绝 `unverified` 来源；数据槽放行任意来源（含 untrusted 备注）。"""
    if param.slot is SlotKind.CONTROL and value.trust is not Trust.TRUSTED:
        raise ForbiddenParamError(
            f"控制槽参数 {param.name} 拒绝 {value.trust.value} 来源的值（H3 输入零信任）"
        )
    return value.raw
