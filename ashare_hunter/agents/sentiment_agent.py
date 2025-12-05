"""情绪面分析师：把资讯、资金、动量与机构关注度汇总为可核验的市场情绪信号。

设计原则与 TradingAgents 的情绪分析师一致，但所有输入都来自本系统其他 Agent
的真实结构化输出；缺失维度从分母剔除并明确披露，绝不凭空生成情绪数值。
"""

from .fundamental_base import score_label


def _yi(value):
    return "数据不足" if value is None else f"{value / 1e8:.2f}亿元"


class SentimentAgent:
    """输出 0-100 情绪评分、情绪状态与逐维度证据。"""

    MOMENTUM_MAP = {
        "动量偏强": 78,
        "短线偏热": 52,   # 偏热不是利好，按中性略偏上处理
        "中性": 50,
        "动量偏弱": 25,
    }

    def run(self, news=None, capital=None, momentum=None, technical=None):
        news = news if isinstance(news, dict) else {}
        capital = capital if isinstance(capital, dict) else {}
        momentum = momentum if isinstance(momentum, dict) else {}
        technical = technical if isinstance(technical, dict) else {}

        dimensions = {}

        # 1. 资讯情绪：正面/负面线索条数差
        bullish = news.get("bullish_count")
        bearish = news.get("bearish_count")
        if news.get("status") == "完成" and bullish is not None and bearish is not None:
            score = max(20, min(85, 50 + (bullish - bearish) * 8))
            dimensions["news"] = {
                "score": score,
                "evidence": [f"相关线索 {len(news.get('items') or [])} 条",
                             f"正面 {bullish} 条 / 负面 {bearish} 条"],
                "label": score_label(score),
            }

        # 2. 资金情绪：主力净流入方向，优先最近一日，回退近 N 日累计
        flow = capital.get("main_net_inflow_latest")
        flow_basis = "最近一日主力净额"
        if flow is None:
            flow = capital.get("main_net_inflow_cumulative")
            flow_basis = "近 N 日主力累计净额"
        if flow is not None:
            magnitude = 10 if abs(flow) >= 1e8 else 0
            score = 70 + magnitude if flow > 0 else (30 - magnitude if flow < 0 else 50)
            dimensions["capital"] = {
                "score": score,
                "evidence": [f"{flow_basis} {_yi(flow)}"],
                "label": score_label(score),
            }

        # 3. 价格动量情绪：用动量 Agent 的离散状态，避免重复计算指标
        state = momentum.get("momentum")
        if state in self.MOMENTUM_MAP:
            score = self.MOMENTUM_MAP[state]
            evidence = [f"动量状态：{state}"]
            if momentum.get("rsi14") is not None:
                evidence.append(f"RSI(14) {momentum['rsi14']:.1f}")
            dimensions["momentum"] = {
                "score": score, "evidence": evidence, "label": score_label(score),
            }

        # 4. 机构关注度：只有评级数量无方向，作为弱正向信号并明确口径
        rating_count = capital.get("analyst_rating_count")
        if rating_count:
            dimensions["attention"] = {
                "score": 55,
                "evidence": [f"分析师评级记录 {rating_count} 条（仅反映关注度，不代表买入方向）"],
                "label": score_label(55),
            }

        available = list(dimensions.values())
        score = round(sum(d["score"] for d in available) / len(available), 1) if available else None
        coverage = round(len(available) / 4, 3)
        if score is None:
            sentiment = "数据不足"
        elif score >= 65:
            sentiment = "偏乐观"
        elif score >= 45:
            sentiment = "中性"
        else:
            sentiment = "偏悲观"

        evidence = [item for d in available for item in d["evidence"]]
        missing = [name for name in ("news", "capital", "momentum", "attention") if name not in dimensions]
        conclusion = (
            f"市场情绪：{sentiment}"
            + (f"（{score:.1f}/100）" if score is not None else "")
            + f"；覆盖 {coverage:.0%} 的情绪维度。"
        )
        if evidence:
            conclusion += " 依据：" + "；".join(evidence) + "。"
        if missing:
            conclusion += " 未取得维度：" + "、".join(missing) + "。"
        conclusion += " 情绪为交易行为的即时刻画，不代表基本面价值或未来方向。"

        return {
            "agent": "情绪面 Agent",
            "status": "完成" if available else "数据不足",
            "score": score,
            "sentiment": sentiment,
            "coverage": coverage,
            "dimensions": dimensions,
            "evidence": evidence,
            "conclusion": conclusion,
        }
