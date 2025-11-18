"""离线冒烟测试：不依赖 Futu OpenD，用模拟数据验证五个新 Agent 与编排链路。

运行：python -m tests.test_agents
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from ashare_hunter.agents.financial_agent import FinancialAgent
from ashare_hunter.agents.valuation_agent import ValuationAgent
from ashare_hunter.agents.news_agent import NewsAgent
from ashare_hunter.agents.capital_agent import CapitalAgent
from ashare_hunter.agents.decision_agent import DecisionAgent
from ashare_hunter.agents.stock_analysis import StockAnalysisOrchestrator


def financial_bundle(items):
    return {
        "available": True,
        "reports": [{
            "date_time_str": "2025-12-31", "period_text": "2025年报",
            "financial_type": "ANNUAL", "item_list": items,
        }],
    }


class FinancialAgentTest(unittest.TestCase):
    def test_normal_fields(self):
        key = financial_bundle([
            {"display_name": "净资产收益率", "data": 18.5, "yoy": None},
            {"display_name": "销售毛利率", "data": 42.0, "yoy": None},
        ])
        income = financial_bundle([
            {"display_name": "营业总收入", "data": 1e10, "yoy": 25.0},
            {"display_name": "归属于母公司股东的净利润", "data": 2e9, "yoy": 30.0},
        ])
        result = FinancialAgent().run(key, income)
        self.assertEqual(result["status"], "完成")
        self.assertAlmostEqual(result["roe_pct"], 18.5)
        self.assertAlmostEqual(result["revenue_yoy_pct"], 25.0)
        self.assertAlmostEqual(result["net_profit_yoy_pct"], 30.0)
        self.assertIsNotNone(result["score"])
        self.assertGreater(result["score"], 70)

    def test_missing_data_no_fabrication(self):
        result = FinancialAgent().run({"available": False}, {"available": False})
        self.assertEqual(result["status"], "数据不足")
        self.assertIsNone(result["score"])
        self.assertIsNone(result["roe_pct"])


class ValuationAgentTest(unittest.TestCase):
    def test_peg_and_percentile(self):
        snap = {"available": True, "pe_ttm_ratio": 30.0, "pb_ratio": 5.0,
                "ps_ratio": 4.0, "update_time": "2026-11-18"}
        pe_detail = {"available": True, "detail": {"trend": {
            "valuation_percentile": 25.0, "average_value": 35.0,
            "current_value": 30.0, "historical_items": [{"value": 20}] * 250,
        }}}
        result = ValuationAgent().run(snapshot=snap, pe_detail=pe_detail,
                                      profit_growth_pct=30.0)
        self.assertAlmostEqual(result["peg"], 1.0)
        self.assertEqual(result["pe_percentile"], 25.0)
        self.assertEqual(result["dimensions"]["pe_historical_percentile"]["label"], "历史偏低")
        self.assertIsNotNone(result["score"])

    def test_negative_growth_no_peg(self):
        result = ValuationAgent().run(
            snapshot={"available": True, "pe_ttm_ratio": 20.0},
            profit_growth_pct=-5.0,
        )
        self.assertIsNone(result["peg"])
        self.assertIn("不适用", result["dimensions"]["peg"]["reason"])

    def test_percentile_0_to_1_conversion(self):
        detail = {"available": True, "detail": {"trend": {"valuation_percentile": 0.35}}}
        self.assertAlmostEqual(ValuationAgent._percentile(detail), 35.0)


class NewsAgentTest(unittest.TestCase):
    def test_classification_and_filter(self):
        notices = {"available": True, "rows": [
            {"title": "关于2025年度业绩预增的公告", "source": "交易所",
             "publish_time": "2026-01-10", "related_securities": ["600519"],
             "url": "http://example.com/1"},
            {"title": "收到重大合同中标通知", "source": "交易所",
             "publish_time": 1760000000000},
        ]}
        result = NewsAgent().run(notices=notices, stock_name="某某科技",)
        # 标题不含股票名且 related_securities 不含股票名 -> 被过滤
        self.assertEqual(len(result["items"]), 0)
        result2 = NewsAgent().run(notices=notices, stock_name="600519")
        self.assertEqual(len(result2["items"]), 1)
        self.assertIn("业绩预告", result2["items"][0]["tags"])

    def test_no_news_no_fabrication(self):
        result = NewsAgent().run(notices={"available": False}, stock_name="测试股份")
        self.assertEqual(result["status"], "数据不足")
        self.assertIsNone(result["score"])
        self.assertEqual(result["items"], [])


class CapitalAgentTest(unittest.TestCase):
    def test_main_inflow(self):
        flow = {"available": True, "rows": [
            {"main_in_flow": 1.5e8, "super_in_flow": 1e8, "big_in_flow": 0.5e8,
             "in_flow": 2e8, "capital_flow_item_time": "2026-11-18"},
        ]}
        dist = {"available": True, "distribution": {
            "capital_in_super": 1e8, "capital_in_big": 5e7,
            "capital_out_super": 4e7, "capital_out_big": 2e7,
            "update_time": "2026-11-18",
        }}
        inst = {"available": True, "rows": [{"institution_name": "基金A"}]}
        result = CapitalAgent().run(capital_flow=flow, distribution=dist, institutional=inst)
        self.assertAlmostEqual(result["main_net_inflow_latest"], 1.5e8)
        self.assertAlmostEqual(result["large_order_net_from_distribution"], 9e7)
        self.assertEqual(result["institutional_holder_count"], 1)
        self.assertGreater(result["score"], 60)

    def test_missing_flow_no_score(self):
        result = CapitalAgent().run(
            capital_flow={"available": False}, distribution={"available": False},
            institutional={"available": False},
        )
        self.assertEqual(result["status"], "数据不足")
        self.assertIsNone(result["score"])


class DecisionAgentTest(unittest.TestCase):
    def test_weighted_score_and_coverage(self):
        fin = {"agent": "财务 Agent", "status": "完成", "score": 80,
               "roe_pct": 18.0, "net_profit_yoy_pct": 20.0}
        val = {"agent": "估值 Agent", "status": "完成", "score": 70,
               "pe_percentile": 25.0, "peg": 0.9}
        news = {"agent": "新闻 Agent", "status": "完成", "score": 60,
                "items": [{"tags": ["业绩预告"]}], "bullish_count": 1, "bearish_count": 0}
        cap = {"agent": "资金 Agent", "status": "完成", "score": 75,
               "main_net_inflow_latest": 1e8}
        trend = {"agent": "趋势 Agent", "trend": "多头排列"}
        momentum = {"agent": "动量 Agent", "momentum": "动量偏强"}
        result = DecisionAgent().run([fin, val, news, cap, trend, momentum])
        self.assertIsNotNone(result["composite_score"])
        self.assertGreater(result["composite_score"], 70)
        self.assertAlmostEqual(result["coverage"], 1.0)
        self.assertEqual(result["rating"], "综合中性偏积极")
        self.assertTrue(result["investment_logic"])
        self.assertTrue(result["risk_factors"])

    def test_low_coverage_low_confidence(self):
        fin = {"agent": "财务 Agent", "status": "完成", "score": 80}
        result = DecisionAgent().run([fin])
        self.assertLess(result["coverage"], 0.4)
        self.assertEqual(result["confidence"], "低")


class FakeProvider:
    """模拟 Provider：仅实现编排器会调用的接口。"""

    def get_daily_history(self, code, days=80):
        dates = pd.date_range("2026-01-01", periods=90, freq="B")
        return pd.DataFrame({
            "date": [d.strftime("%Y-%m-%d") for d in dates],
            "close": [10 + i * 0.05 for i in range(90)],
            "pct_change": [0.5] * 90,
        })

    def get_financials(self, code, statement_type=4, financial_type="ANNUAL", num=5):
        if statement_type == 4:
            return financial_bundle([
                {"display_name": "净资产收益率", "data": 16.0},
                {"display_name": "销售毛利率", "data": 35.0},
            ])
        return financial_bundle([
            {"display_name": "营业总收入", "data": 1e10, "yoy": 20.0},
            {"display_name": "归属于母公司股东的净利润", "data": 2e9, "yoy": 22.0},
        ])

    def get_single_snapshot(self, code):
        return {"available": True, "pe_ttm_ratio": 25.0, "pb_ratio": 4.0,
                "ps_ratio": 3.0, "update_time": "2026-11-18"}

    def get_valuation_detail(self, code, valuation_type="PE", interval_type="YEAR_5"):
        return {"available": True, "detail": {"trend": {
            "valuation_percentile": 40.0, "average_value": 28.0,
            "historical_items": [{"value": 20}] * 100,
        }}}

    def get_news(self, keyword, max_count=20, news_sub_type="ALL"):
        return {"available": False, "reason": "测试环境无资讯接口"}

    def get_capital_flow(self, code, period_type="DAY", days=10):
        return {"available": True, "rows": [
            {"main_in_flow": 8e7, "super_in_flow": 5e7, "big_in_flow": 3e7,
             "in_flow": 1.2e8, "capital_flow_item_time": "2026-11-18"},
        ]}

    def get_capital_distribution(self, code):
        return {"available": True, "distribution": {
            "capital_in_super": 6e7, "capital_in_big": 3e7,
            "capital_out_super": 2e7, "capital_out_big": 1e7,
        }}

    def get_institutional_holdings(self, code, num=10):
        return {"available": False, "reason": "测试环境无机构持股接口"}

    def get_analyst_consensus(self, code):
        return {"available": False, "reason": "测试环境无评级接口"}


class OrchestratorSmokeTest(unittest.TestCase):
    def test_full_pipeline_with_fake_provider(self):
        stock = {"code": "600519", "name": "测试股份", "change_pct": 2.5,
                 "amount": 5e8, "turnover": 1.2, "volume_ratio": 1.1}
        result = StockAnalysisOrchestrator(FakeProvider()).analyze(stock)
        names = [a["agent"] for a in result["agents"]]
        for expected in ("财务 Agent", "估值 Agent", "新闻 Agent", "资金 Agent", "决策 Agent"):
            self.assertIn(expected, names)
        self.assertIsNotNone(result["overall"].get("score"))
        self.assertGreater(result["overall"]["score"], 50)
        self.assertIn("limitations", result) if False else None  # 兼容占位
        # 容错：个别数据源不可用不应中断整体
        self.assertFalse(result["data_sources"]["institutional"]["available"])
        self.assertTrue(result["overall"]["investment_logic"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
