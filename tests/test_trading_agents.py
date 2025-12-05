"""TradingAgents 四层流水线的离线测试：不依赖 OpenD，用模拟 Provider 验证全链路。

运行：python -m tests.test_trading_agents
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ashare_hunter.agents.sentiment_agent import SentimentAgent
from ashare_hunter.agents.research_debate_agent import ResearchDebateAgent
from ashare_hunter.agents.trader_agent import TraderAgent
from ashare_hunter.agents.risk_management_agent import RiskManagementAgent
from ashare_hunter.agents.trading_agents_orchestrator import TradingAgentsOrchestrator
from tests.test_agents import FakeProvider


class SentimentAgentTest(unittest.TestCase):
    def test_multi_dimension_sentiment(self):
        result = SentimentAgent().run(
            news={"status": "完成", "bullish_count": 4, "bearish_count": 1,
                  "items": [{"title": "x"}] * 5},
            capital={"main_net_inflow_latest": 1.2e8, "analyst_rating_count": 3},
            momentum={"momentum": "动量偏强", "rsi14": 62.0},
        )
        self.assertEqual(result["status"], "完成")
        self.assertEqual(result["coverage"], 1.0)
        self.assertEqual(result["sentiment"], "偏乐观")
        self.assertGreater(result["score"], 60)
        self.assertIn("attention", result["dimensions"])

    def test_missing_dimensions_excluded(self):
        result = SentimentAgent().run(capital={"main_net_inflow_latest": -1e8})
        self.assertEqual(result["coverage"], 0.25)
        self.assertEqual(result["sentiment"], "偏悲观")
        self.assertLess(result["score"], 45)
        self.assertIn("未取得维度", result["conclusion"])

    def test_no_data_no_fabrication(self):
        result = SentimentAgent().run()
        self.assertEqual(result["status"], "数据不足")
        self.assertIsNone(result["score"])
        self.assertEqual(result["evidence"], [])


class ResearchDebateAgentTest(unittest.TestCase):
    def test_bull_verdict_and_two_rounds(self):
        reports = {
            "财务 Agent": {"score": 85, "roe_pct": 20.0, "net_profit_yoy_pct": 25.0},
            "估值 Agent": {"pe_percentile": 15.0, "peg": 0.7},
            "趋势 Agent": {"trend": "多头排列"},
        }
        result = ResearchDebateAgent().run(reports, 80.0, 0.9)
        self.assertEqual(result["judge"]["verdict"], "偏多")
        self.assertEqual(result["judge"]["winner"], "多头研究员")
        self.assertGreater(result["bull_case"]["power"], result["bear_case"]["power"])
        self.assertEqual(len(result["rounds"]), 2)
        self.assertTrue(result["investment_plan"]["entry_conditions"])

    def test_bear_verdict(self):
        reports = {
            "财务 Agent": {"score": 35, "net_profit_yoy_pct": -40.0},
            "估值 Agent": {"pe_percentile": 90.0, "peg": 3.0},
            "趋势 Agent": {"trend": "空头排列"},
            "风险 Agent": {"risk_flags": ["近20日最大回撤超过 10%"]},
            "新闻 Agent": {"items": [{"tags": ["风险事项"]}]},
        }
        result = ResearchDebateAgent().run(reports, 30.0, 0.9)
        self.assertEqual(result["judge"]["verdict"], "偏空")
        self.assertTrue(any(e["source"] == "风险" for e in result["bear_case"]["arguments"]))

    def test_no_evidence_no_verdict(self):
        result = ResearchDebateAgent().run({}, None, None)
        self.assertEqual(result["judge"]["verdict"], "证据不足")
        self.assertEqual(result["status"], "数据不足")
        self.assertEqual(result["rounds"], [])


class TraderAgentTest(unittest.TestCase):
    @staticmethod
    def _debate(verdict):
        return {
            "judge": {"verdict": verdict},
            "investment_plan": {"thesis": "研究假设", "time_horizon": "中线（1-3个月）"},
            "bull_case": {"arguments": [{"text": "ROE 较高"}]},
            "bear_case": {"arguments": [{"text": "估值分位偏高"}]},
        }

    def test_buy_sell_hold_matrix(self):
        agent = TraderAgent()
        buy = agent.run(debate=self._debate("偏多"), composite_score=75, coverage=0.9)
        self.assertEqual(buy["action"], "买入")
        self.assertEqual(buy["action_code"], "BUY")
        sell = agent.run(debate=self._debate("偏空"), composite_score=35, coverage=0.9)
        self.assertEqual(sell["action"], "卖出")
        low_coverage = agent.run(debate=self._debate("偏多"), composite_score=75, coverage=0.3)
        self.assertEqual(low_coverage["action"], "持有")
        unconfirmed = agent.run(debate=self._debate("偏多"), composite_score=52, coverage=0.9)
        self.assertEqual(unconfirmed["action"], "持有")

    def test_entry_zone_uses_real_moving_averages(self):
        reports = {"趋势 Agent": {"ma5": 31.0, "ma20": 29.0}}
        result = TraderAgent().run(
            debate=self._debate("偏多"), composite_score=75, coverage=0.9,
            analyst_reports=reports, stock={"last_price": 30.0},
        )
        self.assertIn("MA5", result["entry_zone"])
        self.assertIn("31.00", result["entry_zone"])
        self.assertTrue(result["invalidation"])


class RiskManagementAgentTest(unittest.TestCase):
    @staticmethod
    def _debate(verdict="偏多"):
        return {
            "judge": {"verdict": verdict},
            "bull_case": {"arguments": [{"text": "ROE 较高"}]},
            "bear_case": {"arguments": [{"text": "估值分位偏高"}]},
        }

    def test_high_risk_reduces_position(self):
        risky_reports = {"风险 Agent": {
            "annualized_volatility_pct": 70.0, "max_drawdown_20d_pct": -20.0,
        }}
        trader = {"action": "买入", "confidence": "高"}
        risky = RiskManagementAgent().run(
            trader=trader, debate=self._debate(), analyst_reports=risky_reports, coverage=0.9,
        )
        calm = RiskManagementAgent().run(
            trader=trader, debate=self._debate(), analyst_reports={}, coverage=0.9,
        )
        self.assertEqual(risky["risk_level"], "高")
        self.assertIn("附条件通过", risky["strategy_review"]["result"])
        self.assertLess(risky["position_size"]["max_pct"], calm["position_size"]["max_pct"])
        self.assertTrue(any(c["level"] == "高" for c in risky["risk_checks"]))

    def test_review_rejects_conflicts(self):
        agent = RiskManagementAgent()
        conflict = agent.run(trader={"action": "买入", "confidence": "中"},
                             debate=self._debate("偏空"), coverage=0.9)
        self.assertEqual(conflict["strategy_review"]["result"], "驳回")
        thin_data = agent.run(trader={"action": "买入", "confidence": "中"},
                              debate=self._debate("偏多"), coverage=0.3)
        self.assertEqual(thin_data["strategy_review"]["result"], "驳回")
        self.assertTrue(thin_data["final_decision"]["position"].startswith("0%"))
        self.assertEqual(thin_data["position_size"]["max_pct"], 0)


class TradingAgentsPipelineTest(unittest.TestCase):
    def test_all_four_layers_present(self):
        stock = {"code": "600519", "name": "测试股份", "change_pct": 2.5, "amount": 5e8}
        result = TradingAgentsOrchestrator(FakeProvider()).analyze(stock)
        ta = result["trading_agents"]
        for key in ("analysts", "sentiment", "research", "trading", "risk", "final_recommendation"):
            self.assertIn(key, ta)
        self.assertIn("情绪面 Agent", ta["analysts"])
        self.assertEqual(len(ta["research"]["rounds"]), 2)
        self.assertIn(ta["trading"]["action"], ("买入", "持有", "卖出"))
        final = ta["final_recommendation"]
        self.assertTrue(final["position"])
        self.assertIn(final["risk_level"], ("高", "中", "低"))
        self.assertEqual(result["overall"]["final_action"], final["action"])
        self.assertTrue(result["overall"]["final_position"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
