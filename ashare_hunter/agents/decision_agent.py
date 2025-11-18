"""决策 Agent：综合评分、投资逻辑、风险因素。

只汇总其他 Agent 真实输出的结构化信号，不新增任何未经数据源核验的事实。
缺失维度在 coverage 中披露，综合分按可用维度加权平均并给出置信度。
"""

from .fundamental_base import score_label

# 默认权重（可经 config 覆盖）；技术面由趋势/动量 Agent 映射得到。
DEFAULT_WEIGHTS = {
    "财务": 0.25,
    "估值": 0.25,
    "新闻": 0.15,
    "资金": 0.20,
    "技术面": 0.15,
}


class DecisionAgent:
    """把多 Agent 结果汇总为可追溯的综合评分与逻辑清单。"""

    def __init__(self, weights=None):
        self.weights = dict(DEFAULT_WEIGHTS)
        if isinstance(weights, dict):
            for key, value in weights.items():
                if key in self.weights:
                    try:
                        weight = float(value)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= weight <= 1:
                        self.weights[key] = weight

    def run(self, agent_results, market=None, sectors=None, stock=None, weights=None):
        effective_weights = dict(self.weights)
        if isinstance(weights, dict):
            for key, value in weights.items():
                if key in effective_weights:
                    try:
                        w = float(value)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= w <= 1:
                        effective_weights[key] = w

        by_name = {a.get("agent"): a for a in agent_results or [] if isinstance(a, dict)}
        tech_score = self._technical_score(by_name)

        dimension_scores = {
            "财务": by_name.get("财务 Agent", {}).get("score"),
            "估值": by_name.get("估值 Agent", {}).get("score"),
            "新闻": by_name.get("新闻 Agent", {}).get("score"),
            "资金": by_name.get("资金 Agent", {}).get("score"),
            "技术面": tech_score,
        }
        dimension_status = {
            name: (by_name.get(f"{name} Agent", {}).get("status") if name != "技术面"
                   else ("完成" if tech_score is not None else "数据不足"))
            for name in dimension_scores
        }

        available = {k: v for k, v in dimension_scores.items() if v is not None}
        total_weight = sum(effective_weights.get(k, 0) for k in available)
        composite = None
        if total_weight > 0:
            composite = round(
                sum(available[k] * effective_weights.get(k, 0) for k in available) / total_weight, 1
            )
        coverage = round(total_weight / sum(effective_weights.values()), 3) if effective_weights else 0.0

        rating, confidence = self._rating(composite, coverage)

        logic = self._build_logic(by_name, dimension_scores, market, sectors)
        risks = self._build_risks(by_name, dimension_scores, coverage, market)

        conclusion_parts = [
            f"综合评分 {composite:.1f}/100（{rating}）" if composite is not None else "综合评分：数据不足",
            f"维度覆盖 {coverage:.0%}",
            f"置信度 {confidence}",
        ]
        conclusion = "；".join(conclusion_parts) + "。"
        if composite is None:
            conclusion += " 可用数据不足以形成综合评分，不给出方向性判断。"
        conclusion += " 评分为规则化研究信号，不构成投资建议。"

        return {
            "agent": "决策 Agent", "status": "完成" if composite is not None else "数据不足",
            "score": composite, "composite_score": composite, "rating": rating,
            "confidence": confidence, "coverage": coverage,
            "dimension_scores": dimension_scores, "dimension_status": dimension_status,
            "weights_used": effective_weights,
            "investment_logic": logic, "risk_factors": risks,
            "conclusion": conclusion,
        }

    @staticmethod
    def _technical_score(by_name):
        trend = by_name.get("趋势 Agent", {}).get("trend")
        momentum = by_name.get("动量 Agent", {}).get("momentum")
        base = {"多头排列": 85, "震荡偏强": 65, "震荡偏弱": 40, "空头排列": 25}.get(trend)
        mom = {"动量偏强": 80, "短线偏热": 55, "中性": 55, "动量偏弱": 30}.get(momentum)
        if base is None and mom is None:
            return None
        if base is None:
            return mom
        if mom is None:
            return base
        return round(base * 0.6 + mom * 0.4, 1)

    @staticmethod
    def _rating(composite, coverage):
        if composite is None:
            return "无法判断", "低"
        if coverage < 0.4:
            return "数据不足，暂不判断", "低"
        confidence = "高" if coverage >= 0.8 else ("中" if coverage >= 0.55 else "低")
        if composite >= 75:
            return "综合偏积极", confidence
        if composite >= 60:
            return "综合中性偏积极", confidence
        if composite >= 45:
            return "综合中性", confidence
        if composite >= 30:
            return "综合中性偏谨慎", confidence
        return "综合偏谨慎", confidence

    @staticmethod
    def _build_logic(by_name, dimension_scores, market, sectors):
        logic = []
        fin = by_name.get("财务 Agent", {})
        if fin.get("roe_pct") is not None:
            logic.append(f"ROE {fin['roe_pct']:.2f}%（{score_label(dimension_scores.get('财务'))}）")
        if fin.get("revenue_yoy_pct") is not None:
            logic.append(f"营收同比 {fin['revenue_yoy_pct']:.2f}%")
        if fin.get("net_profit_yoy_pct") is not None:
            logic.append(f"净利润同比 {fin['net_profit_yoy_pct']:.2f}%")
        val = by_name.get("估值 Agent", {})
        if val.get("pe_percentile") is not None:
            logic.append(f"PE 历史分位 {val['pe_percentile']:.1f}%")
        if val.get("peg") is not None:
            logic.append(f"PEG {val['peg']:.2f}")
        cap = by_name.get("资金 Agent", {})
        if cap.get("main_net_inflow_latest") is not None:
            logic.append(f"主力资金净额 {cap['main_net_inflow_latest'] / 1e8:.4f} 亿元")
        news = by_name.get("新闻 Agent", {})
        if news.get("items"):
            tags = [t for item in news["items"] for t in item.get("tags", []) if t != "一般信息"]
            if tags:
                logic.append("公告事件标签：" + "、".join(sorted(set(tags))[:5]))
        trend = by_name.get("趋势 Agent", {}).get("trend")
        if trend:
            logic.append(f"技术趋势：{trend}")
        if market and market.get("market_regime"):
            logic.append(f"市场环境：{market['market_regime']}")
        if not logic:
            logic.append("各维度可用的量化依据不足，暂无可列示的正面投资逻辑。")
        return logic

    @staticmethod
    def _build_risks(by_name, dimension_scores, coverage, market):
        risks = []
        risk_agent = by_name.get("风险 Agent", {})
        for flag in risk_agent.get("risk_flags") or []:
            risks.append(flag)
        for name in ("财务", "估值", "新闻", "资金", "技术面"):
            if dimension_scores.get(name) is None:
                risks.append(f"{name}维度数据不足，结论覆盖不完整")
        if coverage and coverage < 0.6:
            risks.append(f"综合评分仅覆盖 {coverage:.0%} 的预设维度，结论稳健性有限")
        val = by_name.get("估值 Agent", {})
        if val.get("pe_percentile") is not None and val["pe_percentile"] >= 70:
            risks.append(f"PE 处于历史 {val['pe_percentile']:.1f}% 分位，存在估值回归压力")
        fin = by_name.get("财务 Agent", {})
        if fin.get("net_profit_yoy_pct") is not None and fin["net_profit_yoy_pct"] < 0:
            risks.append(f"净利润同比 {fin['net_profit_yoy_pct']:.2f}%，盈利下滑需关注")
        news = by_name.get("新闻 Agent", {})
        if news.get("bearish_count", 0) > news.get("bullish_count", 0):
            risks.append("相关资讯中负面关键词线索多于正面，需核实公告原文")
        if not risks:
            risks.append("基于当前可用数据未触发特定风险阈值；不代表不存在风险")
        return risks
