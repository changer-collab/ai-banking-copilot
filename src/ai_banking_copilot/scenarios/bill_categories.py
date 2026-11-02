"""账单消费分类：非消费判定 + MCC / 一级 / 二级分类的**演示映射**。

仓库账本只有一级 ``category`` 字段，没有真实 MCC。本模块用商户关键词给出
MCC 与二级分类，用于演示「按 MCC 初始分类 + 用户可修正」的产品形态；
**不是**银行真实 MCC 数据，也不据此推断任何用户占比。

用户修正优先于关键词映射；修正是按 ``merchant_key`` 记忆的元数据，不写银行账本。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NON_CONSUMPTION_CATEGORIES = frozenset(
    {"转账", "信用卡还款", "还款", "内部转账", "理财", "投资", "收入", "工资"}
)
"""不计入消费分析的分类：转账、还款、自己转自己、理财申赎、工资等收入。

消费分析只回答「钱花在商品/服务上」；这些资金流动单列，避免扭曲分类画像。
"""

_BRACKET_SEGMENT = re.compile(r"【[^】]*】|\[[^\]]*\]|\([^)]*\)|（[^）]*）")
_BRACKET_CHAR = re.compile(r"[【】\[\]()（）]")
_NON_NAME_CHAR = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_merchant(raw: str) -> str:
    """商户归一：去括号备注、只留中英文数字，用于分组与去重。"""
    text = _BRACKET_SEGMENT.sub("", raw)
    text = _BRACKET_CHAR.sub("", text)
    text = _NON_NAME_CHAR.sub("", text.lower())
    return text or "未知商户"


def display_merchant(raw: str) -> str:
    """展示用商户名：去括号备注、压缩空白、截断到 12 字。"""
    text = _BRACKET_SEGMENT.sub("", raw)
    text = _BRACKET_CHAR.sub("", text)
    text = _WHITESPACE.sub(" ", text).strip()
    return text[:12] or "未知商户"


@dataclass(frozen=True)
class CategoryInfo:
    """一笔消费的分类结果；``mcc`` 是演示映射，不是银行真实 MCC。"""

    category_l1: str
    category_l2: str
    mcc: str
    mcc_label: str


@dataclass(frozen=True)
class CategoryOption:
    """前端「改分类」下拉的选项。"""

    category_l1: str
    category_l2: str
    mcc: str
    mcc_label: str


# 商户关键词 → 分类。顺序重要：更具体的关键词放前面。
_MERCHANT_RULES: tuple[tuple[tuple[str, ...], CategoryInfo], ...] = (
    (("外卖",), CategoryInfo("餐饮", "外卖", "5812", "餐饮服务")),
    (("咖啡", "茶饮", "奶茶"), CategoryInfo("餐饮", "咖啡茶饮", "5814", "快餐茶饮")),
    (("零食", "便利店", "超市便利"), CategoryInfo("餐饮", "零食饮品", "5411", "超市便利")),
    (("聚餐", "餐厅", "食堂", "夜宵", "药房"), CategoryInfo("餐饮", "堂食", "5812", "餐饮服务")),
    (("云购物", "商城", "数码", "电子"), CategoryInfo("购物", "数码", "5732", "电子产品")),
    (("超市", "日用", "生鲜", "水果"), CategoryInfo("购物", "日用百货", "5411", "超市便利")),
    (("服饰", "服装", "鞋"), CategoryInfo("购物", "服饰", "5651", "服饰鞋帽")),
    (("地铁", "公交", "出行"), CategoryInfo("交通", "公共交通", "4111", "公共交通")),
    (("打车", "网约车", "出租"), CategoryInfo("交通", "网约车", "4121", "网约车")),
    (("加油", "汽油"), CategoryInfo("交通", "加油", "5541", "加油站")),
    (("房租", "租金"), CategoryInfo("居住", "房租", "6513", "房租")),
    (("水电", "燃气"), CategoryInfo("居住", "水电燃气", "4900", "公共事业")),
    (("物业",), CategoryInfo("居住", "物业", "6513", "物业")),
    (("宽带", "话费", "通讯"), CategoryInfo("生活缴费", "通讯", "4899", "通讯服务")),
    (("视频会员", "音乐会员", "云盘会员"), CategoryInfo("娱乐", "影音会员", "7994", "数字内容")),
    (("电影", "演出"), CategoryInfo("娱乐", "电影演出", "7922", "电影演出")),
    (("游戏",), CategoryInfo("娱乐", "游戏", "7994", "数字内容")),
)

_L1_DEFAULTS: dict[str, CategoryInfo] = {
    "餐饮": CategoryInfo("餐饮", "其他餐饮", "5812", "餐饮服务"),
    "购物": CategoryInfo("购物", "其他购物", "5999", "零售"),
    "交通": CategoryInfo("交通", "其他交通", "4111", "交通服务"),
    "住房": CategoryInfo("居住", "其他居住", "6513", "居住服务"),
    "居住": CategoryInfo("居住", "其他居住", "6513", "居住服务"),
    "生活缴费": CategoryInfo("生活缴费", "其他缴费", "4900", "公共事业"),
    "娱乐": CategoryInfo("娱乐", "其他娱乐", "7994", "数字内容"),
}

_FALLBACK = CategoryInfo("其他", "其他", "0000", "未分类")


def is_consumption_category(category: str) -> bool:
    """该账本分类是否属于消费分析（转账/还款/理财不属于）。"""
    return category not in NON_CONSUMPTION_CATEGORIES


def category_info(
    merchant: str, category: str, override: str | None = None
) -> CategoryInfo:
    """商户 → 分类。用户修正（二级分类）优先，其次关键词，最后一级兜底。"""
    if override:
        matched = _option_by_l2(override)
        if matched is not None:
            return CategoryInfo(
                matched.category_l1,
                matched.category_l2,
                matched.mcc,
                matched.mcc_label,
            )
    key = normalize_merchant(merchant)
    for keywords, info in _MERCHANT_RULES:
        if any(keyword in key for keyword in keywords):
            return info
    return _L1_DEFAULTS.get(category, _FALLBACK)


def _option_by_l2(category_l2: str) -> CategoryOption | None:
    for option in CATEGORY_OPTIONS:
        if option.category_l2 == category_l2:
            return option
    return None


CATEGORY_OPTIONS: tuple[CategoryOption, ...] = (
    CategoryOption("餐饮", "外卖", "5812", "餐饮服务"),
    CategoryOption("餐饮", "堂食", "5812", "餐饮服务"),
    CategoryOption("餐饮", "咖啡茶饮", "5814", "快餐茶饮"),
    CategoryOption("餐饮", "零食饮品", "5411", "超市便利"),
    CategoryOption("餐饮", "其他餐饮", "5812", "餐饮服务"),
    CategoryOption("购物", "数码", "5732", "电子产品"),
    CategoryOption("购物", "日用百货", "5411", "超市便利"),
    CategoryOption("购物", "服饰", "5651", "服饰鞋帽"),
    CategoryOption("购物", "生鲜", "5411", "超市便利"),
    CategoryOption("购物", "其他购物", "5999", "零售"),
    CategoryOption("交通", "公共交通", "4111", "公共交通"),
    CategoryOption("交通", "网约车", "4121", "网约车"),
    CategoryOption("交通", "加油", "5541", "加油站"),
    CategoryOption("交通", "其他交通", "4111", "交通服务"),
    CategoryOption("居住", "房租", "6513", "房租"),
    CategoryOption("居住", "水电燃气", "4900", "公共事业"),
    CategoryOption("居住", "物业", "6513", "物业"),
    CategoryOption("居住", "其他居住", "6513", "居住服务"),
    CategoryOption("生活缴费", "通讯", "4899", "通讯服务"),
    CategoryOption("生活缴费", "其他缴费", "4900", "公共事业"),
    CategoryOption("娱乐", "影音会员", "7994", "数字内容"),
    CategoryOption("娱乐", "电影演出", "7922", "电影演出"),
    CategoryOption("娱乐", "游戏", "7994", "数字内容"),
    CategoryOption("娱乐", "其他娱乐", "7994", "数字内容"),
    CategoryOption("其他", "其他", "0000", "未分类"),
)
"""前端可选二级分类；MCC 为演示映射。"""


def category_options() -> tuple[CategoryOption, ...]:
    """按一级分类分组前的稳定顺序返回全部二级分类。"""
    return CATEGORY_OPTIONS


def category_option_payload() -> list[dict[str, str]]:
    """接口序列化：前端只用于下拉，不参与金额计算。"""
    return [
        {
            "category_l1": item.category_l1,
            "category_l2": item.category_l2,
            "mcc": item.mcc,
            "mcc_label": item.mcc_label,
        }
        for item in CATEGORY_OPTIONS
    ]


def is_valid_category(category_l2: str) -> bool:
    """用户修正只接受已登记的二级分类，避免自由文本污染画像。"""
    return any(item.category_l2 == category_l2 for item in CATEGORY_OPTIONS)
