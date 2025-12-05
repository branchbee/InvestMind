"""Trader Agent：把研究辩论与综合评分转化为可执行的交易研究建议。

输出买入 / 持有 / 卖出三档建议、置信度、关注区间与失效条件。
关注区间由真实均线数据推导，不生成预测性目标价；卖出对空仓者表示回避。
"""


class TraderAgent:
    """第三层：交易决策。只基于研究辩论与分析师报告，不引入新数据。"""

    ACTION_CODE = {"买入": "BUY", "持有": "HOLD", "卖出": "SELL"}

    def run(self, debate=None, analyst_reports=None, composite_score=None,
            coverage=None, stock=None):
        debate = debate if isinstance(debate, dict) else {}
        reports = analyst_reports if isinstance(analyst_reports, dict) else {}
        stock = stock if isinstance(stock, dict) else {}

        verdict = (debate.get("judge") or {}).get("verdict") or "证据不足"
        plan = debate.get("investment_plan") or {}
        bull_args = (debate.get("bull_case") or {}).get("arguments") or []
        bear_args = (debate.get("bear_case") or {}).get("arguments") or []

        action, reason = self._decide(verdict, composite_score, coverage)
        confidence = self._confidence(composite_score, coverage, len(bull_args) + len(bear_args))
        zone = self._entry_zone(action, stock, reports)
        invalidation = [e["text"] for e in bear_args[:3]] or ["暂无可核验的反向信号"]
        invalidation.append(self._score_invalidation(action))

        thesis = (
            f"研究裁决为{verdict}。{plan.get('thesis', '')}"
            + (f" 核心依据：{bull_args[0]['text']}" if bull_args and action == "买入" else "")
            + (f" 主要顾虑：{bear_args[0]['text']}" if bear_args and action == "卖出" else "")
        )

        return {
            "agent": "交易 Agent",
            "status": "完成",
            "action": action,
            "action_code": self.ACTION_CODE[action],
            "confidence": confidence,
            "conviction_score": composite_score,
            "verdict": verdict,
            "reason": reason,
            "thesis": thesis,
            "entry_zone": zone,
            "invalidation": invalidation,
            "time_horizon": plan.get("time_horizon", "未确定"),
            "conclusion": (
                f"交易建议：{action}（{self.ACTION_CODE[action]}），置信度 {confidence}。"
                f" 决策依据：{reason} 关注区间：{zone}。"
                " 卖出建议对空仓者表示回避，不等同于持有空头；本建议为研究假设，不构成投资指令。"
            ),
        }

    @staticmethod
    def _decide(verdict, composite_score, coverage):
        if coverage is not None and coverage < 0.4:
            return "持有", "维度覆盖不足 40%，不足以支撑方向性交易决策"
        if verdict == "证据不足":
            return "持有", "多空双方均无可核验证据，维持观望"
        if verdict.startswith("偏多"):
            if composite_score is not None and composite_score >= 60:
                return "买入", "研究结论偏多且综合评分达到 60 分以上"
            return "持有", "研究结论偏多但综合评分未确认，等待进一步验证"
        if verdict.startswith("偏空"):
            if composite_score is not None and composite_score <= 50:
                return "卖出", "研究结论偏空且综合评分低于 50 分"
            return "持有", "研究结论偏空但综合评分未确认，暂不建议减仓"
        return "持有", "多空证据均势，无明确方向性优势"

    @staticmethod
    def _confidence(composite_score, coverage, evidence_count):
        if composite_score is None or coverage is None:
            return "低"
        if composite_score >= 70 and coverage >= 0.75 and evidence_count >= 4:
            return "高"
        if composite_score >= 55 and coverage >= 0.55:
            return "中"
        return "低"

    @staticmethod
    def _entry_zone(action, stock, reports):
        trend = reports.get("趋势 Agent") or {}
        ma5, ma20 = trend.get("ma5"), trend.get("ma20")
        price = stock.get("last_price") or stock.get("price") or trend.get("last_close")
        if isinstance(ma5, (int, float)) and isinstance(ma20, (int, float)):
            low, high = min(ma5, ma20), max(ma5, ma20)
            if action == "买入":
                return f"回踩 MA5 {ma5:.2f} / MA20 {ma20:.2f} 区间（{low:.2f}-{high:.2f}）可关注"
            if action == "卖出":
                return f"若跌破 MA20 {ma20:.2f}，应执行回避或减仓"
            return f"现价参考 MA5 {ma5:.2f} / MA20 {ma20:.2f}，等待方向确认"
        if isinstance(price, (int, float)):
            return f"仅有现价 {price:.2f}，缺少均线数据，不提供具体区间"
        return "行情数据不足，不提供价格区间"

    @staticmethod
    def _score_invalidation(action):
        if action == "买入":
            return "综合评分跌破 50 或研究裁决转为偏空时，建议作废"
        if action == "卖出":
            return "综合评分回升至 60 以上且研究裁决转为偏多时，重新评估"
        return "综合评分突破 70 或跌破 40 时，重新评估方向"
