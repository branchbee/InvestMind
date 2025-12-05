"""风险管理层：风险评估、仓位评估与策略审核。

TradingAgents 的第四层。用可核验的风险信号计算风险等级与仓位上限，
并对 Trader Agent 的建议做一致性审核；所有调整都给出明确依据。
"""

CHECK_PENALTY = {"高": 25, "中": 12, "低": 0}
CONFIDENCE_FACTOR = {"高": 1.2, "中": 1.0, "低": 0.6}
RISK_FACTOR = {"低": 1.0, "中": 0.7, "高": 0.4}


class RiskManagementAgent:
    """第四层：风险经理 + 投资组合经理，输出最终执行决策。"""

    def run(self, trader=None, debate=None, analyst_reports=None,
            composite_score=None, coverage=None):
        trader = trader if isinstance(trader, dict) else {}
        debate = debate if isinstance(debate, dict) else {}
        reports = analyst_reports if isinstance(analyst_reports, dict) else {}

        action = trader.get("action") or "持有"
        confidence = trader.get("confidence") or "低"
        verdict = (debate.get("judge") or {}).get("verdict") or "证据不足"
        bull_args = (debate.get("bull_case") or {}).get("arguments") or []
        bear_args = (debate.get("bear_case") or {}).get("arguments") or []

        checks = self._build_checks(reports, coverage)
        penalty = sum(CHECK_PENALTY[c["level"]] for c in checks)
        risk_score = max(0, 100 - penalty)
        high_count = sum(1 for c in checks if c["level"] == "高")
        if high_count >= 2 or penalty >= 60:
            risk_level = "高"
        elif high_count >= 1 or penalty >= 25:
            risk_level = "中"
        else:
            risk_level = "低"

        review, review_reasons = self._review(action, verdict, coverage, risk_level)
        position = self._position(action, confidence, coverage, risk_level, review)
        conditions = self._conditions(reports, action, risk_level)
        risk_debate = self._risk_debate(bull_args, bear_args, checks, position, risk_level)

        return {
            "agent": "风险管理 Agent",
            "status": "完成",
            "risk_level": risk_level,
            "risk_score": risk_score,
            "risk_checks": checks,
            "risk_debate": risk_debate,
            "position_size": position,
            "strategy_review": {"result": review, "reasons": review_reasons},
            "final_decision": {
                "action": action,
                "position": position["recommended"],
                "review": review,
                "conditions": conditions,
            },
            "conclusion": (
                f"风险等级：{risk_level}（{risk_score}/100）；"
                f"建议仓位：{position['recommended']}；策略审核：{review}。"
                " 仓位为研究上限而非建仓指令，须结合个人风险承受能力与持仓集中度独立决策。"
            ),
        }

    @staticmethod
    def _build_checks(reports, coverage):
        checks = []

        def add(name, level, detail):
            checks.append({"name": name, "level": level, "detail": detail})

        risk = reports.get("风险 Agent") or {}
        vol = risk.get("annualized_volatility_pct")
        if isinstance(vol, (int, float)):
            level = "高" if vol >= 50 else ("中" if vol >= 35 else "低")
            add("波动率", level, f"近20日年化波动率 {vol:.1f}%")
        drawdown = risk.get("max_drawdown_20d_pct")
        if isinstance(drawdown, (int, float)):
            level = "高" if drawdown <= -15 else ("中" if drawdown <= -10 else "低")
            add("回撤", level, f"近20日最大回撤 {drawdown:.1f}%")

        val = reports.get("估值 Agent") or {}
        pct = val.get("pe_percentile")
        if isinstance(pct, (int, float)):
            level = "高" if pct >= 85 else ("中" if pct >= 70 else "低")
            add("估值分位", level, f"PE 处于历史 {pct:.1f}% 分位")

        fin = reports.get("财务 Agent") or {}
        profit_yoy = fin.get("net_profit_yoy_pct")
        if isinstance(profit_yoy, (int, float)) and profit_yoy < 0:
            level = "高" if profit_yoy <= -30 else "中"
            add("盈利质量", level, f"净利润同比 {profit_yoy:.1f}%")

        news = reports.get("新闻 Agent") or {}
        tags = {t for item in (news.get("items") or []) for t in (item.get("tags") or [])}
        if "风险事项" in tags:
            add("事件风险", "中", "公告标题命中风险事项关键词，须核实原文")

        if coverage is not None and coverage < 0.6:
            add("数据覆盖", "中" if coverage >= 0.4 else "高",
                f"综合评分仅覆盖 {coverage:.0%} 的预设维度")

        liquidity = reports.get("流动性 Agent") or {}
        amount = liquidity.get("amount")
        if isinstance(amount, (int, float)) and amount < 5e7:
            add("流动性", "中", f"成交额 {amount / 1e8:.2f} 亿元，低于 0.5 亿元参考线")
        return checks

    @staticmethod
    def _position(action, confidence, coverage, risk_level, review="通过"):
        if review == "驳回":
            return {
                "recommended": "0%（策略审核未通过）", "max_pct": 0,
                "basis": "策略审核驳回，不提供仓位",
            }
        base = {"买入": 20.0, "持有": 10.0, "卖出": 0.0}.get(action, 10.0)
        cov = coverage if isinstance(coverage, (int, float)) else 0.5
        raw = base * CONFIDENCE_FACTOR.get(confidence, 0.6) * cov * RISK_FACTOR.get(risk_level, 0.4)
        if review == "附条件通过":
            raw *= 0.6
        raw = max(0.0, min(25.0, raw))
        low, high = int(round(raw * 0.7)), int(round(raw))
        if high == 0:
            recommended = "0%（暂不参与）"
        elif low == high:
            recommended = f"{high}%"
        else:
            recommended = f"{low}-{high}%"
        basis = (f"基准 {base:.0f}% × 置信度 {CONFIDENCE_FACTOR.get(confidence, 0.6)}"
                 f" × 覆盖 {cov:.0%} × 风险系数 {RISK_FACTOR.get(risk_level, 0.4)}")
        if review == "附条件通过":
            basis += " × 附条件折减 0.6"
        return {
            "recommended": recommended,
            "max_pct": high,
            "basis": basis,
        }

    @staticmethod
    def _review(action, verdict, coverage, risk_level):
        reasons = []
        result = "通过"
        if coverage is not None and coverage < 0.4:
            result = "驳回"
            reasons.append("维度覆盖不足 40%，策略依据不充分")
        if action == "买入" and verdict.startswith("偏空"):
            result = "驳回"
            reasons.append("交易方向与偏空研究裁决冲突")
        if action == "卖出" and verdict.startswith("偏多"):
            result = "驳回"
            reasons.append("交易方向与偏多研究裁决冲突")
        if action == "买入" and risk_level == "高" and result != "驳回":
            result = "附条件通过"
            reasons.append("风险等级偏高，须降低仓位并设置止损")
        if not reasons:
            reasons.append("交易方向与研究裁决一致，风险敞口可接受")
        return result, reasons

    @staticmethod
    def _conditions(reports, action, risk_level):
        conditions = []
        if action == "买入":
            conditions.append("单笔浮亏达到 8% 时执行退出复核")
        if risk_level == "高":
            conditions.append("波动率回落至 35% 以下前不加仓")
        val = reports.get("估值 Agent") or {}
        if isinstance(val.get("pe_percentile"), (int, float)) and val["pe_percentile"] >= 70:
            conditions.append("PE 历史分位升破 70% 时重新评估")
        fin = reports.get("财务 Agent") or {}
        if isinstance(fin.get("net_profit_yoy_pct"), (int, float)) and fin["net_profit_yoy_pct"] < 0:
            conditions.append("下一期财报确认盈利拐点后再加仓")
        if not conditions:
            conditions.append("按周复核评分与风险信号，出现新风险事件即时重估")
        return conditions

    @staticmethod
    def _risk_debate(bull_args, bear_args, checks, position, risk_level):
        high_checks = [c["detail"] for c in checks if c["level"] == "高"]
        return {
            "aggressive": (
                f"激进视角：{bull_args[0]['text'] if bull_args else '多头证据有限'}。"
                f" 当前风险等级 {risk_level}，可承担 {position['recommended']} 的研究敞口。"
            ),
            "conservative": (
                f"保守视角：{bear_args[0]['text'] if bear_args else '空头证据有限'}。"
                + (f" 高风险项：{'；'.join(high_checks)}。" if high_checks else " 未触发高风险项。")
                + " 建议以本金安全为首要约束。"
            ),
            "neutral": (
                f"中性视角：多空证据 {len(bull_args)} / {len(bear_args)} 条，"
                f"风险等级 {risk_level}，建议分批而非一次性建仓，仓位以 {position['recommended']} 为上限。"
            ),
        }
