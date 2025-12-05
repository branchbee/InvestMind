"""TradingAgents 四层编排器。

复用现有 StockAnalysisOrchestrator 完成数据拉取与基础分析师计算，再依次执行：
第一层 分析师团队（基本面/估值/新闻/资金/情绪/技术）
第二层 多空研究辩论（Bull / Bear / Research Manager）
第三层 交易决策（Trader Agent）
第四层 风险管理（风险评估 / 仓位评估 / 策略审核）

每层结果独立保留，上层只消费下层的结构化输出，任一层缺失数据均明确标记。
"""

from .stock_analysis import StockAnalysisOrchestrator
from .sentiment_agent import SentimentAgent
from .research_debate_agent import ResearchDebateAgent
from .trader_agent import TraderAgent
from .risk_management_agent import RiskManagementAgent


class TradingAgentsOrchestrator:
    """把单股分析升级为完整的多层投研决策流水线。"""

    def __init__(self, provider=None):
        self.base_orchestrator = StockAnalysisOrchestrator(provider)

    def analyze(self, stock, market=None, sectors=None):
        base = self.base_orchestrator.analyze(stock, market, sectors)
        by_name = {
            a.get("agent"): a for a in base.get("agents", [])
            if isinstance(a, dict) and a.get("agent")
        }

        # 第一层补充：情绪面分析师（消费已有分析师的结构化输出，不额外取数）
        sentiment = SentimentAgent().run(
            news=by_name.get("新闻 Agent"),
            capital=by_name.get("资金 Agent"),
            momentum=by_name.get("动量 Agent"),
            technical=by_name.get("趋势 Agent"),
        )
        analysts = dict(by_name)
        analysts["情绪面 Agent"] = sentiment

        decision = by_name.get("决策 Agent") or {}
        composite = decision.get("composite_score")
        coverage = decision.get("coverage")

        # 第二层：多空研究辩论
        debate = ResearchDebateAgent().run(
            analysts, composite_score=composite, coverage=coverage
        )
        # 第三层：交易决策
        trader = TraderAgent().run(
            debate=debate, analyst_reports=analysts,
            composite_score=composite, coverage=coverage, stock=stock,
        )
        # 第四层：风险管理
        risk = RiskManagementAgent().run(
            trader=trader, debate=debate, analyst_reports=analysts,
            composite_score=composite, coverage=coverage,
        )

        final = {
            "action": trader.get("action"),
            "action_code": trader.get("action_code"),
            "confidence": trader.get("confidence"),
            "position": (risk.get("final_decision") or {}).get("position"),
            "risk_level": risk.get("risk_level"),
            "review": (risk.get("strategy_review") or {}).get("result"),
            "conditions": (risk.get("final_decision") or {}).get("conditions") or [],
            "verdict": (debate.get("judge") or {}).get("verdict"),
        }

        result = dict(base)
        result["trading_agents"] = {
            "analysts": analysts,
            "sentiment": sentiment,
            "research": debate,
            "trading": trader,
            "risk": risk,
            "final_recommendation": final,
        }
        result["overall"] = {
            **(base.get("overall") or {}),
            "final_action": final["action"],
            "final_position": final["position"],
            "final_risk_level": final["risk_level"],
        }
        return result
