"""理财 ⑤ 取证闭环：把 §6.4 里错答的 12 条原话钉成回归用例。

覆盖三类修复：
1. **事实直答**（风险测评等级 / 收益 / 持仓对账）——确定性判据，不经模型、不推算盈亏；
2. **先拒后问**（等级不匹配时不等补金额就给拒绝与替代）与**产品槽文本解析**；
3. **能力边界**（第三人账户）与**受信任选中 + 明确动词**的确定性合成意图。
"""

from __future__ import annotations

from dataclasses import replace

from ai_banking_copilot.planning.llm import Intent, IntentProposal
from ai_banking_copilot.web.app import AppDeps
from ai_banking_copilot.web.session import Session, SessionState


class _LowConfidenceSubscribe:
    """模型只给出低置信申购意图：同句必须由确定性合成接管。"""

    def propose(self, utterance: str, context: object) -> IntentProposal:
        return IntentProposal(
            intents=(
                Intent(
                    action="wealth.subscribe", slots={}, confidence=0.1, evidence=utterance
                ),
            ),
            raw=utterance,
        )



def _card_text(deps: AppDeps, utterance: str, selected: dict[str, str]) -> tuple[str, object]:
    view = Session(deps).submit(utterance, selected=selected)
    assert view.card is not None, view.message
    return "\n".join(view.card.lines), view


def test_risk_level_question_is_answered_from_the_risk_store(deps: AppDeps) -> None:
    """「我风险等级多少」⇒ 直答本人测评事实，不再掉进产品目录。"""
    view = Session(deps).submit("我风险等级多少")
    assert view.state is SessionState.IDLE
    assert view.card is None
    assert view.message.startswith("你的风险测评等级：R3")
    assert "演示 seed 数据（非真实测评）" in view.message


def test_income_question_answers_holdings_with_unknown_income(deps: AppDeps) -> None:
    """「收益多少」⇒ 持仓金额来自网关，收益无净值数据 ⇒ 如实标未知。"""
    view = Session(deps).submit("收益多少")
    assert view.state is SessionState.IDLE
    assert view.card is None
    assert "活期理财篮子" in view.message
    assert "稳利债券 A" in view.message
    assert "当前收益 未知（演示环境无净值数据，不推算盈亏）" in view.message


def test_earnings_question_shares_the_same_unknown_income_answer(deps: AppDeps) -> None:
    """「我赚了多少」与「收益多少」同一条口径，不得编造盈亏。"""
    view = Session(deps).submit("我赚了多少")
    assert view.state is SessionState.IDLE
    assert "未知（演示环境无净值数据，不推算盈亏）" in view.message
    assert "赚" not in view.message.replace("不推算盈亏", "")


def test_holdings_dispute_is_answered_with_gateway_facts(deps: AppDeps) -> None:
    """「持仓对不上」⇒ 摊开网关事实逐条对账，不再回「未知意图」。"""
    view = Session(deps).submit("持仓对不上")
    assert view.state is SessionState.IDLE
    assert view.message.startswith("你的理财持仓（演示数据）：")
    assert view.message.count("- ") == 2


def test_third_party_account_is_a_capability_boundary_not_a_policy_rejection(
    deps: AppDeps,
) -> None:
    """「用我老婆账户买」⇒ 能力边界话术；它不该被当成策略拒绝累计熔断。"""
    session = Session(deps)
    view = session.submit("用我老婆账户买")
    assert view.state is SessionState.BLOCKED
    assert "能力边界" in view.message
    assert "仅支持操作你本人名下的账户与银行卡" in view.message
    assert session._policy_rejections == 0  # noqa: SLF001 - 直接验证不计入触发 4


def test_balance_tamper_with_wealth_action_is_a_capability_boundary(
    deps: AppDeps,
) -> None:
    """「把余额改成 100 万再去买理财」⇒ 整句拒绝；不得用伪造余额生成申购卡（S6-7）。"""
    session = Session(deps)
    view = session.submit("帮我把账户余额改成 100 万，再去买理财")
    assert view.state is SessionState.BLOCKED
    assert view.card is None
    assert view.plan is None
    assert "不能修改账户余额" in view.message
    assert "申购或赎回理财" in view.message
    assert session._policy_rejections == 0  # noqa: SLF001 - 能力边界不计入触发 4


def test_balance_tamper_without_wealth_action_keeps_the_old_path(deps: AppDeps) -> None:
    """只「改余额」不带理财动作 ⇒ 不套理财话术，仍按原路径拒绝（不影响他人场景）。"""
    view = Session(deps).submit("把余额改成 100 万")
    assert view.state is SessionState.BLOCKED
    assert "不能修改账户余额" not in view.message


def test_risk_mismatch_is_refused_before_asking_for_the_amount(deps: AppDeps) -> None:
    """「我想买 R4」⇒ 先拒后问：测评 R3 买不了 R4，先给拒绝与可申购替代。"""
    view = Session(deps).submit("我想买 R4")
    assert view.state is SessionState.BLOCKED
    assert view.card is None
    assert "风险等级不匹配：测评 R3 < 产品 R4" in view.message
    assert "均衡配置 B" in view.message


def test_risk_level_in_the_utterance_selects_the_product(deps: AppDeps) -> None:
    """「买 1000 元 R2」⇒ 选中 R2 的稳利债券 A，金额不再被 R2 污染。"""
    text, view = _card_text(deps, "买 1000 元 R2", {"account": "acc_savings"})
    assert "稳利债券 A" in text
    assert "活期理财篮子" not in text
    assert "1000" in text
    assert view.plan is not None


def test_trusted_product_plus_bare_amount_builds_the_intent(deps: AppDeps) -> None:
    """「买 1000 元」+ 页面选中 wp_bond ⇒ 受信任选中 + 明确动词合成申购意图。"""
    text, _ = _card_text(
        deps, "买 1000 元", {"account": "acc_savings", "product": "wp_bond"}
    )
    assert "稳利债券 A" in text
    assert "活期理财篮子" not in text


def test_synthetic_intent_does_not_report_a_contradictory_drop(deps: AppDeps) -> None:
    """模型给了低置信申购意图、合成接管后，不得再说「另有意图未采纳」。"""
    session = Session(replace(deps, intent_model=_LowConfidenceSubscribe()))
    view = session.submit(
        "买 1000 元", selected={"account": "acc_savings", "product": "wp_bond"}
    )
    assert view.card is not None
    assert "未采纳" not in view.message


def test_redeem_by_spoken_product_name_resolves_the_product(deps: AppDeps) -> None:
    """「赎回 300 元稳利债券A」⇒ 产品名去空格命中，不再追问「请选择理财产品」。"""
    view = Session(deps).submit("赎回 300 元稳利债券A", selected={"account": "acc_savings"})
    assert view.clarifications == ()
    text = "\n".join(view.card.lines) if view.card is not None else ""
    assert "稳利债券 A" in text
