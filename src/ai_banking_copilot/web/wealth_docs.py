"""理财四份演示文件与风险揭示抄录文案。

**诚实性声明**：以下均为演示文本，不是正式法律文件，不构成任何投资建议或收益承诺。
前端必须同时显示「演示文本，非正式法律文件」。
"""

from __future__ import annotations

DECLARATION_TEXT = "本人已充分理解产品风险，自愿承担相关投资风险"
"""首次购买 R3 及以上产品时需要逐字抄录的声明。"""

DOCUMENTS: tuple[dict[str, object], ...] = (
    {
        "id": "prospectus",
        "title": "产品说明书",
        "version": "demo-v1",
        "is_demo": True,
        "body": (
            "演示文本，非正式法律文件。\n"
            "本产品为演示理财产品，产品类型、风险等级、期限、起购金额与费率均来自演示数据。"
            "产品不保证本金，不保证收益；业绩比较基准不等于实际收益。"
            "申购前请确认资金在封闭期内不需要动用。"
        ),
    },
    {
        "id": "risk_disclosure",
        "title": "风险揭示书",
        "version": "demo-v1",
        "is_demo": True,
        "body": (
            "演示文本，非正式法律文件。\n"
            "理财非存款，产品有风险，投资须谨慎。产品可能因市场波动发生本金损失；"
            "封闭期内不可赎回，可能影响资金流动性；信用风险、利率风险与流动性风险由投资者承担。"
            "请根据自身风险承受能力审慎决策。"
        ),
    },
    {
        "id": "investor_rights",
        "title": "投资者权益须知",
        "version": "demo-v1",
        "is_demo": True,
        "body": (
            "演示文本，非正式法律文件。\n"
            "投资者有权在产品销售前获取产品说明书、风险揭示书与费率信息；"
            "有权查询本人持仓与交易记录；有权在开放期按产品规则申请赎回。"
            "本演示环境不接入真实投诉、回访或监管投诉渠道。"
        ),
    },
    {
        "id": "sales_agreement",
        "title": "销售协议",
        "version": "demo-v1",
        "is_demo": True,
        "body": (
            "演示文本，非正式法律文件。\n"
            "本协议仅用于演示交易确认流程。申购与赎回均需通过服务端计划确认与一次性验证码；"
            "演示环境不产生真实资金划转，不接入真实支付、双录或人工回访。"
        ),
    },
)

DOCUMENT_IDS = tuple(str(item["id"]) for item in DOCUMENTS)


def documents_payload() -> dict[str, dict[str, object]]:
    """前端契约：`{doc_id: {title, version, is_demo, body}}`。"""
    return {str(item["id"]): dict(item) for item in DOCUMENTS}
