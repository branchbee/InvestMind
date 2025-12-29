"""四层多空辩论证据的离线回归测试：不依赖 OpenD，全部使用模拟数据。

运行：python -m tests.test_debate_evidence
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LLM_ENABLED", "false")

import pandas as pd

from ashare_hunter.agents.financial_agent import FinancialAgent
from ashare_hunter.agents.news_agent import NewsAgent
from ashare_hunter.agents.stock_analysis import TrendAgent, MomentumAgent
from ashare_hunter.agents.debate_evidence import build_evidence, LAYERS
from ashare_hunter.agents.research_debate_agent import ResearchDebateAgent
from ashare_hunter.agents.trading_agents_orchestrator import TradingAgentsOrchestrator
from tests.test_agents import FakeProvider


def financial_bundle(periods):
    """构造多期年报，按 Futu 的「最新在前」顺序返回。"""
    reports = []
    for period, roe, gross, revenue, profit in reversed(periods):
        reports.append({
            "date_time_str": period, "period_text": period[:4] + "年报",
            "financial_type": "ANNUAL",
            "item_list": [
                {"display_name": "净资产收益率", "data": roe, "yoy": None},
                {"display_name": "销售毛利率", "data": gross, "yoy": None},
                {"display_name": "营业总收入", "data": revenue, "yoy": None},
                {"display_name": "归属于母公司股东的净利润", "data": profit, "yoy": None},
            ],
        })
    return {"available": True, "reports": reports}


GROWING = [
    ("2023-12-31", 15.0, 38.0, 100.0, 10.0),
    ("2024-12-31", 17.0, 40.0, 120.0, 13.0),
    ("2025-12-31", 19.0, 42.0, 150.0, 18.0),
]

DECLINING = [
    ("2023-12-31", 20.0, 45.0, 150.0, 20.0),
    ("2024-12-31", 17.0, 41.0, 130.0, 16.0),
    ("2025-12-31", 13.0, 36.0, 110.0, 11.0),
]


def jump_history(with_volume=True):
    """59 根横盘 + 最后一根跳涨：可同时触发 MA20 上穿、MACD 金叉、多头排列。"""
    close = [100.0] * 59 + [112.0]
    data = {
        "date": pd.date_range("2026-08-01", periods=60).strftime("%Y-%m-%d"),
        "close": close,
    }
    if with_volume:
        data["volume"] = [1000.0] * 55 + [3000.0] * 5
    return pd.DataFrame(data)


def news_bundle(titles):
    return {"available": True, "rows": [
        {"title": t, "source": "交易所", "publish_time": "2026-11-15",
         "related_securities": ["600001"], "url": "http://example.com"}
        for t in titles
    ]}


class FinancialTrendTest(unittest.TestCase):
    def test_multi_period_growth_and_improvement(self):
        bundle = financial_bundle(GROWING)
        result = FinancialAgent().run(bundle, bundle)
        trend = result["growth_trend"]
        self.assertEqual(trend["report_count"], 3)
        self.assertEqual(trend["revenue"]["status"], "是")
        self.assertEqual(trend["net_profit"]["status"], "是")
        self.assertEqual(trend["gross_margin"]["direction"], "改善")
        self.assertEqual(trend["roe"]["direction"], "改善")
        self.assertEqual(trend["revenue"]["consecutive_growth"], 2)
        self.assertEqual(trend["revenue"]["as_of"], "2025-12-31")

    def test_declining_series_is_not_growth(self):
        bundle = financial_bundle(DECLINING)
        trend = FinancialAgent().run(bundle, bundle)["growth_trend"]
        self.assertEqual(trend["revenue"]["status"], "否")
        self.assertEqual(trend["net_profit"]["status"], "否")
        self.assertEqual(trend["gross_margin"]["direction"], "恶化")
        self.assertEqual(trend["roe"]["direction"], "恶化")

    def test_single_period_no_fabrication(self):
        bundle = financial_bundle(GROWING[:1])
        result = FinancialAgent().run(bundle, bundle)
        trend = result["growth_trend"]
        self.assertEqual(trend["revenue"]["status"], "数据不足")
        self.assertIsNone(trend["revenue"]["sustained"])
        self.assertEqual(trend["gross_margin"]["direction"], "数据不足")
        evidence = build_evidence({"财务 Agent": result})["items"]
        f1 = next(x for x in evidence if x["id"] == "F1")
        self.assertEqual(f1["answer"], "数据不足")
        self.assertEqual(f1["direction"], "unknown")
        self.assertIsNone(f1["strength"])


class TechnicalIndicatorTest(unittest.TestCase):
    def test_breakout_golden_cross_volume_uptrend(self):
        hist = jump_history()
        tr = TrendAgent().run(hist)
        mom = MomentumAgent().run(hist)
        self.assertTrue(tr["ma20_breakout"])
        self.assertTrue(tr["above_ma20"])
        self.assertTrue(tr["uptrend"])
        self.assertTrue(mom["macd_golden_cross"])
        self.assertFalse(mom["macd_dead_cross"])
        self.assertEqual(mom["volume_status"], "放大")
        self.assertGreater(mom["volume_expansion"], 1.2)
        evidence = build_evidence({"趋势 Agent": tr, "动量 Agent": mom})["by_layer"]["技术面"]
        answers = {x["id"]: x["answer"] for x in evidence}
        self.assertEqual(answers["T1"], "是")
        self.assertEqual(answers["T2"], "是")
        self.assertEqual(answers["T3"], "是")
        self.assertEqual(answers["T4"], "是")
        self.assertTrue(all(x["direction"] == "bull" for x in evidence))

    def test_missing_volume_marked_as_insufficient(self):
        mom = MomentumAgent().run(jump_history(with_volume=False))
        self.assertEqual(mom["volume_status"], "数据不足")
        self.assertIsNone(mom["volume_expansion"])
        t3 = next(x for x in build_evidence({"动量 Agent": mom})["items"] if x["id"] == "T3")
        self.assertEqual(t3["answer"], "数据不足")

    def test_short_history_does_not_create_macd_signal(self):
        hist = jump_history().tail(20).reset_index(drop=True)
        mom = MomentumAgent().run(hist)
        self.assertIsNone(mom["macd_golden_cross"])
        t2 = next(x for x in build_evidence({"动量 Agent": mom})["items"] if x["id"] == "T2")
        self.assertEqual(t2["answer"], "数据不足")


class NewsEventTest(unittest.TestCase):
    def test_four_news_questions_have_traceable_evidence(self):
        titles = [
            "测试股份发布新一代AI芯片",
            "测试股份签订重大订单",
            "AI算力产业规划出台，行业景气度提升",
            "机构上调评级至买入",
        ]
        news = NewsAgent().run(notices=news_bundle(titles), stock_code="600001")
        self.assertTrue(set(news["event_evidence"]) >=
                        {"新产品/新技术", "重大合同/订单", "行业政策/景气", "机构评级上调"})
        evidence = build_evidence({"新闻 Agent": news})["by_layer"]["新闻面"]
        self.assertEqual([x["answer"] for x in evidence], ["是"] * 4)
        self.assertTrue(all("发布" in x["detail"] or "检索到" in x["detail"] for x in evidence))
        self.assertTrue(all(x["strength"] == "中" for x in evidence))

    def test_downgrade_is_bear_evidence(self):
        news = NewsAgent().run(notices=news_bundle(["机构下调评级至卖出"]), stock_code="600001")
        n4 = next(x for x in build_evidence({"新闻 Agent": news})["items"] if x["id"] == "N4")
        self.assertEqual(n4["answer"], "否")
        self.assertEqual(n4["direction"], "bear")

    def test_no_news_does_not_claim_event_absence(self):
        news = NewsAgent().run(stock_code="600001")
        n1 = next(x for x in build_evidence({"新闻 Agent": news})["items"] if x["id"] == "N1")
        self.assertEqual(n1["answer"], "数据不足")
        self.assertIn("未返回不等于事件不存在", n1["detail"])


class MacroEvidenceTest(unittest.TestCase):
    def test_no_macro_source_no_fabrication(self):
        items = build_evidence({}, {})["by_layer"]["宏观面"]
        m1, m2 = items[0], items[1]
        self.assertEqual(m1["answer"], "数据不足")
        self.assertEqual(m2["answer"], "数据不足")
        self.assertIn("不推断利率方向", m1["detail"])
        self.assertIn("景气度不作推断", m2["detail"])
        self.assertEqual([x["direction"] for x in (m1, m2)], ["unknown", "unknown"])

    def test_explicit_macro_input_is_labeled_external(self):
        macro = {"rate": {"judgement": "有利", "value": "10Y 美债 4.1%", "source": "手动录入",
                          "as_of": "2026-11-18"}}
        m1 = build_evidence({}, {"macro": macro})["by_layer"]["宏观面"][0]
        self.assertEqual(m1["answer"], "是")
        self.assertEqual(m1["direction"], "bull")
        self.assertEqual(m1["strength"], "弱")
        self.assertIn("外部录入", m1["source"])
        self.assertIn("非系统自动采集", m1["detail"])

    def test_macro_without_source_is_rejected(self):
        macro = {"rate": {"judgement": "有利"}}
        m1 = build_evidence({}, {"macro": macro})["by_layer"]["宏观面"][0]
        self.assertEqual(m1["answer"], "数据不足")

    def test_sector_proxy_is_weak_and_labeled(self):
        sectors = pd.DataFrame([
            {"sector_name": "AI算力", "change_pct": 3.4},
            {"sector_name": "半导体", "change_pct": 2.1},
        ])
        m2 = build_evidence({}, {"sectors": sectors})["by_layer"]["宏观面"][1]
        self.assertEqual(m2["direction"], "bull")
        self.assertEqual(m2["strength"], "弱")
        self.assertIn("间接代理", m2["detail"])
        self.assertIn("不等于产业景气度", m2["detail"])

    def test_non_tech_sectors_do_not_stand_in_for_ai(self):
        sectors = pd.DataFrame([{"sector_name": "白酒", "change_pct": 5.0}])
        m2 = build_evidence({}, {"sectors": sectors})["by_layer"]["宏观面"][1]
        self.assertEqual(m2["answer"], "数据不足")

    def test_market_regime_is_real_macro_evidence(self):
        market = {"market_regime": "偏多", "advance_decline_ratio": 0.66, "avg_change_pct": 1.2}
        m3 = build_evidence({}, {"market": market})["by_layer"]["宏观面"][2]
        self.assertEqual(m3["answer"], "是")
        self.assertEqual(m3["direction"], "bull")


class DebateIntegrationTest(unittest.TestCase):
    def setUp(self):
        bundle = financial_bundle(GROWING)
        self.reports = {
            "财务 Agent": FinancialAgent().run(bundle, bundle),
            "趋势 Agent": TrendAgent().run(jump_history()),
            "动量 Agent": MomentumAgent().run(jump_history()),
            "新闻 Agent": NewsAgent().run(notices=news_bundle(["测试股份发布新一代AI芯片"]),
                                          stock_code="600001"),
            "资金 Agent": {"main_net_inflow_latest": 1.2e8},
        }

    def test_four_layer_matrix_and_arguments(self):
        result = ResearchDebateAgent().run(self.reports, 78.0, 0.9)
        self.assertEqual(len(result["evidence_matrix"]), 15)
        self.assertEqual(set(result["evidence_by_layer"]), set(LAYERS))
        cov = result["evidence_coverage"]
        self.assertGreater(cov["answered"], 8)
        self.assertGreater(len(result["bull_case"]["arguments"]), 5)
        self.assertIn("四层检查表", result["judge"]["reasoning"])
        self.assertIn("layers", result["rounds"][0])
        layer_names = [x["layer"] for x in result["rounds"][0]["layers"]]
        self.assertIn("基本面", layer_names)
        self.assertIn("技术面", layer_names)
        self.assertIn("新闻面", layer_names)
        self.assertTrue(result["investment_plan"]["evidence_gaps"])

    def test_empty_reports_still_no_verdict(self):
        result = ResearchDebateAgent().run({}, None, None)
        self.assertEqual(result["judge"]["verdict"], "证据不足")
        self.assertEqual(result["status"], "数据不足")
        self.assertEqual(result["rounds"], [])
        self.assertEqual(len(result["evidence_matrix"]), 15)
        self.assertEqual(result["evidence_coverage"]["answered"], 0)

    def test_one_sided_evidence_triggers_bias_caveat(self):
        result = ResearchDebateAgent().run(self.reports, 80.0, 0.9)
        self.assertTrue(any("确认偏误" in c or "对立面" in c for c in result["caveats"]))

    def test_rebuttal_is_layer_aware(self):
        result = ResearchDebateAgent().run(self.reports, 78.0, 0.9)
        second = result["rounds"][1]
        self.assertTrue(second["bull_rebuttal"])
        self.assertTrue(second["bear_rebuttal"])

    def test_gui_renders_evidence_matrix(self):
        from gui import StockAnalysisDialog
        text = StockAnalysisDialog._debate(ResearchDebateAgent().run(self.reports, 78.0, 0.9))
        self.assertIn("### 四层证据检查表", text)
        self.assertIn("#### 基本面", text)
        self.assertIn("#### 宏观面", text)
        self.assertIn("尚缺数据", text)
        self.assertIn("机构上调评级", text)

    def test_orchestrator_passes_context_into_debate(self):
        stock = {"code": "600001", "name": "测试股份", "amount": 1e8, "change_pct": 1.2}
        result = TradingAgentsOrchestrator(FakeProvider()).analyze(stock)
        debate = result["trading_agents"]["research"]
        self.assertEqual(len(debate["evidence_matrix"]), 15)
        self.assertIn("evidence_coverage", debate)
        self.assertIn("caveats", debate)


if __name__ == "__main__":
    unittest.main(verbosity=2)
