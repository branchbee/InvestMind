"""agents 决策层补齐测试：评分/证据/交易/风控的边界与上下文注入。

覆盖现有测试未触及的分支：
- DecisionAgent 权重过滤、评级分档、维度覆盖、投资逻辑与风险清单；
- CapitalAgent 主力回退、评分档位、累计与文案分支；
- NewsAgent 事件分类、时间格式、代码规范化、相关性与条数上限；
- ValuationAgent PEG 前提、分位口径与 PB/时间字段；
- FinancialAgent 英文财报别名、同比回退、增长/改善趋势边界；
- SentimentAgent 累计资金回退与情绪分档；
- TraderAgent 决策矩阵、置信度、关注区间与失效条件；
- RiskManagementAgent 审核冲突、检查项、仓位与三方观点；
- DebateEvidence 负向证据、板块/宏观兼容输入；
- ResearchDebateAgent 均势裁决与审慎提示；
- TradingAgentsOrchestrator 的 market / sectors / macro 上下文透传。

全部离线运行。运行：python -m tests.test_agent_decision_edges
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LLM_ENABLED", "false")

import pandas as pd

from ashare_hunter.agents.capital_agent import CapitalAgent
from ashare_hunter.agents.decision_agent import DecisionAgent
from ashare_hunter.agents.debate_evidence import build_evidence, _rows, _sample_text
from ashare_hunter.agents.financial_agent import FinancialAgent, ALIASES
from ashare_hunter.agents.news_agent import NewsAgent
from ashare_hunter.agents.research_debate_agent import ResearchDebateAgent
from ashare_hunter.agents.risk_management_agent import RiskManagementAgent
from ashare_hunter.agents.sentiment_agent import SentimentAgent
from ashare_hunter.agents.stock_analysis import TrendAgent, MomentumAgent
from ashare_hunter.agents.trader_agent import TraderAgent
from ashare_hunter.agents.trading_agents_orchestrator import TradingAgentsOrchestrator
from ashare_hunter.agents.valuation_agent import ValuationAgent
from tests.test_agents import FakeProvider


def report_bundle(periods, fields=("revenue", "profit")):
    """按 Futu「最新在前」顺序构造多期财报。"""
    reports = []
    for period, revenue, profit, gross, roe in periods:
        items = []
        if "revenue" in fields:
            items.append({"display_name": "营业总收入", "data": revenue})
        if "profit" in fields:
            items.append({"display_name": "归属于母公司股东的净利润", "data": profit})
        items += [
            {"display_name": "销售毛利率", "data": gross},
            {"display_name": "净资产收益率", "data": roe},
        ]
        reports.append({"date_time_str": period, "period_text": period[:4] + "年报",
                        "financial_type": "ANNUAL", "item_list": items})
    return {"available": True, "reports": list(reversed(reports))}


class DecisionAgentEdgeTest(unittest.TestCase):
    def test_weights_override_ignores_invalid_values(self):
        agent = DecisionAgent(weights={"财务": 0.5, "未知": 0.9, "估值": "bad",
                                       "新闻": 2.0, "资金": -1.0})
        self.assertEqual(agent.weights["财务"], 0.5)
        self.assertEqual(agent.weights["新闻"], 0.15)
        self.assertEqual(agent.weights["资金"], 0.20)
        result = agent.run([{"agent": "财务 Agent", "score": 80, "status": "完成"}],
                           weights={"技术面": 1.0, "估值": "x"})
        self.assertEqual(result["weights_used"]["技术面"], 1.0)
        self.assertEqual(result["weights_used"]["估值"], 0.25)

    def test_no_scores_returns_data_insufficient(self):
        result = DecisionAgent().run([])
        self.assertIsNone(result["composite_score"])
        self.assertEqual(result["status"], "数据不足")
        self.assertEqual(result["rating"], "无法判断")
        self.assertEqual(result["confidence"], "低")
        self.assertEqual(result["coverage"], 0.0)
        self.assertIn("不给出方向性判断", result["conclusion"])

    def test_rating_all_bands_and_confidence(self):
        cases = [
            ((90, 0.9), ("综合偏积极", "高")),
            ((65, 0.9), ("综合中性偏积极", "高")),
            ((50, 0.9), ("综合中性", "高")),
            ((35, 0.9), ("综合中性偏谨慎", "高")),
            ((20, 0.9), ("综合偏谨慎", "高")),
            ((80, 0.3), ("数据不足，暂不判断", "低")),
            ((60, 0.6), ("综合中性偏积极", "中")),
            ((60, 0.4), ("综合中性偏积极", "低")),
            ((None, 0.9), ("无法判断", "低")),
        ]
        for args, expected in cases:
            self.assertEqual(DecisionAgent._rating(*args), expected, args)

    def test_technical_score_combinations(self):
        both = {"趋势 Agent": {"trend": "多头排列"}, "动量 Agent": {"momentum": "动量偏强"}}
        self.assertEqual(DecisionAgent._technical_score(both), 83.0)
        self.assertEqual(DecisionAgent._technical_score({"趋势 Agent": both["趋势 Agent"]}), 85)
        self.assertEqual(DecisionAgent._technical_score({"动量 Agent": both["动量 Agent"]}), 80)
        self.assertIsNone(DecisionAgent._technical_score({}))

    def test_single_dimension_coverage_and_status(self):
        result = DecisionAgent().run([{"agent": "财务 Agent", "status": "完成", "score": 80}])
        self.assertAlmostEqual(result["coverage"], 0.25)
        self.assertAlmostEqual(result["composite_score"], 80.0)
        self.assertEqual(result["dimension_status"]["财务"], "完成")
        self.assertEqual(result["dimension_status"]["技术面"], "数据不足")
        self.assertTrue(any("综合评分仅覆盖" in r for r in result["risk_factors"]))

    def test_build_logic_includes_every_available_source(self):
        reports = [
            {"agent": "财务 Agent", "score": 80, "roe_pct": 18.0,
             "revenue_yoy_pct": 20.0, "net_profit_yoy_pct": 25.0},
            {"agent": "估值 Agent", "score": 70, "pe_percentile": 25.0, "peg": 0.9},
            {"agent": "资金 Agent", "score": 75, "main_net_inflow_latest": 1e8},
            {"agent": "新闻 Agent", "score": 60,
             "items": [{"tags": ["业绩预告", "一般信息"]}]},
            {"agent": "趋势 Agent", "trend": "多头排列"},
        ]
        logic = DecisionAgent().run(reports, market={"market_regime": "偏多"})["investment_logic"]
        for needle in ("ROE 18.00%", "营收同比", "净利润同比", "PE 历史分位", "PEG",
                       "主力资金净额", "公告事件标签：业绩预告", "技术趋势：多头排列",
                       "市场环境：偏多"):
            self.assertIn(needle, " ".join(logic))

    def test_build_risks_all_threshold_triggers(self):
        reports = [
            {"agent": "风险 Agent", "risk_flags": ["自定义风险信号"]},
            {"agent": "财务 Agent", "score": 30, "net_profit_yoy_pct": -20.0},
            {"agent": "估值 Agent", "score": 30, "pe_percentile": 88.0},
            {"agent": "新闻 Agent", "score": 40, "bearish_count": 3, "bullish_count": 1},
        ]
        risks = " ".join(DecisionAgent().run(reports)["risk_factors"])
        for needle in ("自定义风险信号", "资金维度数据不足", "PE 处于历史 88.0% 分位",
                       "净利润同比 -20.00%", "负面关键词线索多于正面"):
            self.assertIn(needle, risks)

    def test_build_risks_placeholder_when_clean(self):
        reports = [
            {"agent": "财务 Agent", "score": 80},
            {"agent": "估值 Agent", "score": 70, "pe_percentile": 20.0},
            {"agent": "新闻 Agent", "score": 60, "bullish_count": 2, "bearish_count": 0},
            {"agent": "资金 Agent", "score": 75},
            {"agent": "趋势 Agent", "trend": "多头排列"},
            {"agent": "动量 Agent", "momentum": "中性"},
        ]
        result = DecisionAgent().run(reports)
        self.assertAlmostEqual(result["coverage"], 1.0)
        self.assertEqual(len(result["risk_factors"]), 1)
        self.assertIn("未触发特定风险阈值", result["risk_factors"][0])

    def test_non_dict_entries_are_ignored(self):
        result = DecisionAgent().run([None, "x", 42, {}])
        self.assertIsNone(result["composite_score"])
        self.assertEqual(result["status"], "数据不足")


class CapitalAgentEdgeTest(unittest.TestCase):
    @staticmethod
    def _flow(main):
        return {"available": True, "rows": [{"main_in_flow": main}]}

    def test_main_flow_falls_back_to_large_orders(self):
        flow = {"available": True, "rows": [{"super_in_flow": 6e7, "big_in_flow": 4e7}]}
        result = CapitalAgent().run(capital_flow=flow)
        self.assertAlmostEqual(result["main_net_inflow_latest"], 1e8)
        self.assertIn("最近一日主力净流入", result["conclusion"])
        # 只有单个大单字段时无法回算主力净额，文案必须如实说明
        partial = CapitalAgent().run(
            capital_flow={"available": True, "rows": [{"super_in_flow": 6e7}]})
        self.assertIsNone(partial["main_net_inflow_latest"])
        self.assertIn("接口未提供 main_in_flow，仅有分项", partial["conclusion"])

    def test_score_bands_and_institution_bonus(self):
        for value, expected in ((1e8, 75), (5e6, 65), (-1e8, 35), (-5e6, 45), (0, 50)):
            self.assertEqual(CapitalAgent().run(capital_flow=self._flow(value))["score"], expected, value)
        with_inst = CapitalAgent().run(
            capital_flow=self._flow(1e8),
            institutional={"available": True, "rows": [{"institution_name": "基金A"}]},
        )
        self.assertEqual(with_inst["score"], 80)
        self.assertEqual(with_inst["institutional_holder_count"], 1)

    def test_rows_helpers_accept_list_and_unavailable(self):
        self.assertEqual(CapitalAgent._rows([{"a": 1}]), [{"a": 1}])
        self.assertEqual(CapitalAgent._rows({"available": False, "rows": [1]}), [])
        self.assertEqual(CapitalAgent._rows(None), [])
        self.assertEqual(CapitalAgent._yi(None), "数据不足")
        self.assertEqual(CapitalAgent._yi(2e8), "2.0000亿")

    def test_cumulative_and_flow_days(self):
        flow = {"available": True, "rows": [
            {"main_in_flow": 1e7, "in_flow": 2e7}, {"main_in_flow": 2e7, "in_flow": 3e7}]}
        result = CapitalAgent().run(capital_flow=flow)
        self.assertAlmostEqual(result["main_net_inflow_cumulative"], 3e7)
        self.assertEqual(result["flow_days"], 2)
        self.assertIn("近 2 日主力累计", result["conclusion"])

    def test_conclusion_branches_for_unavailable_sources(self):
        result = CapitalAgent().run(
            capital_flow=self._flow(1e8),
            distribution={"available": True, "distribution": {
                "capital_in_super": 1e8, "capital_in_big": 5e7,
                "capital_out_super": 4e7, "capital_out_big": 2e7,
                "update_time": "2026-11-18"}},
            institutional={"available": False, "reason": "无权限"},
            analyst_consensus={"available": False, "reason": "无权限"},
        )
        text = result["conclusion"]
        for needle in ("特大+大单净额", "资金分布更新于 2026-11-18",
                       "机构持股：未取得（无权限）", "分析师评级：未取得"):
            self.assertIn(needle, text)
        self.assertAlmostEqual(result["large_order_net_from_distribution"], 9e7)

    def test_no_data_status(self):
        result = CapitalAgent().run()
        self.assertEqual(result["status"], "数据不足")
        self.assertIsNone(result["score"])
        self.assertIsNone(result["institutional_holder_count"])
        self.assertIsNone(result["analyst_rating_count"])
        self.assertEqual(result["flow_days"], 0)
        self.assertIn("主力资金：数据不足", result["conclusion"])


class NewsAgentEdgeTest(unittest.TestCase):
    def test_classify_multiple_tags_and_default(self):
        tags = NewsAgent._classify("签订重大合同并发布新产品")
        self.assertIn("重大合同/订单", tags)
        self.assertIn("新产品/新技术", tags)
        self.assertEqual(NewsAgent._classify("公司召开例行会议"), ["一般信息"])

    def test_tone_negative_and_neutral(self):
        self.assertEqual(NewsAgent._tone("业绩预减且被立案调查"), "偏负面")
        self.assertEqual(NewsAgent._tone("公司召开例行会议"), "中性")
        self.assertEqual(NewsAgent._tone("业绩预增且回购增持"), "偏正面")

    def test_dedup_and_empty_titles(self):
        notices = {"available": True, "rows": [
            {"title": "相同标题"}, {"title": "相同标题"}, {"title": ""}, {"title": None}]}
        result = NewsAgent().run(notices=notices)
        self.assertEqual(len(result["items"]), 1)

    def test_format_time_variants(self):
        self.assertEqual(NewsAgent._format_time(None), "")
        self.assertEqual(NewsAgent._format_time("2026-11-18"), "2026-11-18")
        self.assertRegex(NewsAgent._format_time("1760000000000"), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertRegex(NewsAgent._format_time("1760000000"), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertEqual(NewsAgent._format_time("x" * 40), "x" * 30)

    def test_normalise_code_variants(self):
        cases = {"SH.600519": "600519", "600519.SH": "600519", "US.AAPL": "AAPL",
                 "AAPL.US": "AAPL", "BRK.B": "BRK.B", "": ""}
        for raw, expected in cases.items():
            self.assertEqual(NewsAgent._normalise_code(raw), expected, raw)

    def test_is_related_without_identifiers_and_by_name(self):
        self.assertTrue(NewsAgent._is_related({"title": "任何标题", "related_securities": []}, "", ""))
        self.assertTrue(NewsAgent._is_related(
            {"title": "贵州茅台发布新产品", "related_securities": []}, "贵州茅台", ""))
        self.assertTrue(NewsAgent._is_related(
            {"title": "x", "related_securities": [{"name": "贵州茅台"}]}, "贵州茅台", ""))
        self.assertTrue(NewsAgent._is_related(
            {"title": "x", "related_securities": ["SH.600519"]}, "贵州茅台", "600519"))
        self.assertFalse(NewsAgent._is_related(
            {"title": "x", "related_securities": ["SZ.000001"]}, "贵州茅台", "600519"))

    def test_event_evidence_samples_capped_at_three(self):
        rows = [{"title": f"发布新产品{i}", "source": "交易所"} for i in range(4)]
        result = NewsAgent().run(notices={"available": True, "rows": rows})
        bucket = result["event_evidence"]["新产品/新技术"]
        self.assertEqual(bucket["count"], 4)
        self.assertEqual(len(bucket["samples"]), 3)

    def test_items_capped_at_twenty(self):
        rows = [{"title": f"标题{i}"} for i in range(25)]
        result = NewsAgent().run(notices={"available": True, "rows": rows})
        self.assertEqual(len(result["items"]), 20)
        self.assertIsNotNone(result["score"])


class ValuationAgentEdgeTest(unittest.TestCase):
    def test_no_data_no_fabrication(self):
        result = ValuationAgent().run()
        self.assertEqual(result["status"], "数据不足")
        self.assertIsNone(result["score"])
        self.assertIn("不生成虚构数值", result["conclusion"])
        self.assertEqual(result["dimensions"]["pe_historical_percentile"]["label"], "数据不足")

    def test_peg_only_when_pe_and_growth_are_positive(self):
        snap = {"available": True, "pe_ttm_ratio": 20.0}
        self.assertIn("PE 缺失或非正", ValuationAgent().run(
            snapshot={"available": True, "pe_ttm_ratio": 0})["dimensions"]["peg"]["reason"])
        self.assertIn("缺少净利润同比增速", ValuationAgent().run(
            snapshot=snap)["dimensions"]["peg"]["reason"])
        self.assertIn("PEG 不适用", ValuationAgent().run(
            snapshot=snap, profit_growth_pct=0)["dimensions"]["peg"]["reason"])
        self.assertAlmostEqual(ValuationAgent().run(
            snapshot=snap, profit_growth_pct=25.0)["peg"], 0.8)

    def test_percentile_variants(self):
        def detail(value):
            return {"available": True, "detail": {"trend": {"valuation_percentile": value}}}

        self.assertAlmostEqual(ValuationAgent._percentile(detail(0.35)), 35.0)
        self.assertAlmostEqual(ValuationAgent._percentile(detail(1)), 100.0)
        self.assertAlmostEqual(ValuationAgent._percentile(detail(45)), 45.0)
        self.assertAlmostEqual(ValuationAgent._percentile(detail("45%")), 45.0)
        self.assertIsNone(ValuationAgent._percentile({"available": False}))
        self.assertIsNone(ValuationAgent._percentile(detail(None)))
        self.assertIsNone(ValuationAgent._percentile(None))

    def test_percentile_label_bands(self):
        agent = ValuationAgent()
        for value, expected in ((None, "数据不足"), (29.9, "历史偏低"), (30, "历史中性"),
                                (70, "历史中性"), (70.1, "历史偏高")):
            self.assertEqual(agent._percentile_label(value), expected)

    def test_history_count_and_data_times(self):
        detail = {"available": True, "detail": {"trend": {
            "valuation_percentile": 40.0, "average_value": 28.0,
            "historical_items": [{"value": 20}] * 7,
            "last_update_time_str": "2026-11-18"}}}
        result = ValuationAgent().run(
            snapshot={"available": True, "pe_ttm_ratio": 30.0, "update_time": "2026-11-19"},
            pe_detail=detail)
        self.assertEqual(result["dimensions"]["pe_historical_percentile"]["history_count"], 7)
        self.assertIn("近5年 PE 均值 28.00", result["conclusion"])
        self.assertIn("估值分位数据时间：2026-11-18", result["conclusion"])
        self.assertIn("快照更新时间：2026-11-19", result["conclusion"])

    def test_pb_percentile_and_trend_helpers(self):
        pb = {"available": True, "detail": {"trend": {"valuation_percentile": 80.0}}}
        result = ValuationAgent().run(snapshot={"available": True, "pe_ttm_ratio": 20.0},
                                      pb_detail=pb)
        self.assertAlmostEqual(result["pb_percentile"], 80.0)
        self.assertIn("PB 历史分位 80.0%", result["conclusion"])
        self.assertEqual(ValuationAgent._history_count({"available": False}), 0)
        self.assertIsNone(ValuationAgent._trend_value({"available": False}, "average_value"))


class FinancialAgentEdgeTest(unittest.TestCase):
    def test_english_aliases_for_us_financials(self):
        bundle = {"available": True, "reports": [{"date_time_str": "2025-12-31", "item_list": [
            {"display_name": "Total Revenue", "data": 1e10},
            {"display_name": "Net Income", "data": 2e9},
            {"display_name": "Return On Equity (ROE)", "data": 18.0},
            {"display_name": "Gross Profit Margin", "data": 42.0},
        ]}]}
        result = FinancialAgent().run(bundle, bundle)
        self.assertEqual(result["status"], "完成")
        self.assertAlmostEqual(result["roe_pct"], 18.0)
        self.assertAlmostEqual(result["gross_margin_pct"], 42.0)
        self.assertAlmostEqual(result["dimensions"]["revenue"]["value"], 1e10)
        self.assertAlmostEqual(result["dimensions"]["net_profit"]["value"], 2e9)

    def test_compute_yoy_fallback_when_interface_omits_yoy(self):
        bundle = report_bundle([
            ("2024-12-31", 100, 20, 38, 15),
            ("2025-12-31", 120, 25, 40, 17),
        ])
        result = FinancialAgent().run(bundle, bundle)
        self.assertAlmostEqual(result["revenue_yoy_pct"], 20.0)
        self.assertAlmostEqual(result["net_profit_yoy_pct"], 25.0)

    def test_growth_partial_and_zero_base(self):
        partial = report_bundle([
            ("2023-12-31", 100, 10, 38, 15),
            ("2024-12-31", 90, 9, 37, 14),
            ("2025-12-31", 95, 9.5, 39, 16),
        ])
        growth = FinancialAgent().run(partial, partial)["growth_trend"]["revenue"]
        self.assertEqual(growth["status"], "部分")
        self.assertFalse(growth["sustained"])
        self.assertEqual(growth["consecutive_growth"], 1)

        zero = report_bundle([
            ("2023-12-31", 0, 0, 38, 15),
            ("2024-12-31", 100, 10, 39, 16),
            ("2025-12-31", 120, 12, 40, 17),
        ])
        trend = FinancialAgent().run(zero, zero)["growth_trend"]["revenue"]
        self.assertEqual(trend["status"], "数据不足")
        self.assertIn(None, trend["period_changes_pct"])

    def test_delta_flat_and_duplicate_period_dedup(self):
        reports = [
            {"date_time_str": "2025-12-31", "item_list": [{"display_name": "销售毛利率", "data": 40.0}]},
            {"date_time_str": "2024-12-31", "item_list": [{"display_name": "销售毛利率", "data": 40.0}]},
            {"date_time_str": "2024-12-31", "item_list": [{"display_name": "销售毛利率", "data": 40.0}]},
        ]
        series = FinancialAgent._series(reports, ALIASES["gross_margin"])
        self.assertEqual(len(series), 2)  # 同一报告期去重，保留最后一次
        delta = FinancialAgent._delta(series)
        self.assertEqual(delta["status"], "部分")
        self.assertEqual(delta["direction"], "持平")
        self.assertEqual(delta["delta_pp"], 0.0)


class SentimentAgentEdgeTest(unittest.TestCase):
    def test_cumulative_flow_fallback_and_magnitude(self):
        positive = SentimentAgent().run(capital={"main_net_inflow_cumulative": 2e8})
        self.assertEqual(positive["dimensions"]["capital"]["score"], 80)
        self.assertIn("近 N 日主力累计净额", " ".join(positive["dimensions"]["capital"]["evidence"]))
        self.assertEqual(SentimentAgent().run(
            capital={"main_net_inflow_latest": -2e8})["dimensions"]["capital"]["score"], 20)
        self.assertEqual(SentimentAgent().run(
            capital={"main_net_inflow_latest": 0})["dimensions"]["capital"]["score"], 50)

    def test_sentiment_bands(self):
        optimistic = SentimentAgent().run(news={"status": "完成", "bullish_count": 5, "bearish_count": 0})
        self.assertEqual(optimistic["sentiment"], "偏乐观")
        self.assertEqual(optimistic["dimensions"]["news"]["score"], 85)
        pessimistic = SentimentAgent().run(capital={"main_net_inflow_latest": -2e8})
        self.assertEqual(pessimistic["sentiment"], "偏悲观")
        neutral = SentimentAgent().run(news={"status": "完成", "bullish_count": 1, "bearish_count": 1})
        self.assertEqual(neutral["sentiment"], "中性")

    def test_non_dict_inputs_do_not_crash(self):
        result = SentimentAgent().run(news=[], capital=None, momentum="x", technical=123)
        self.assertEqual(result["status"], "数据不足")
        self.assertIsNone(result["score"])
        self.assertEqual(result["evidence"], [])


class TraderAgentEdgeTest(unittest.TestCase):
    def test_decide_all_branches(self):
        d = TraderAgent._decide
        self.assertEqual(d("偏多", 75, 0.3)[0], "持有")
        self.assertEqual(d("证据不足", 90, 0.9)[0], "持有")
        self.assertEqual(d("偏多", None, 0.9)[0], "持有")
        self.assertEqual(d("偏空", 60, 0.9)[0], "持有")
        self.assertEqual(d("中性", 70, 0.9)[0], "持有")
        self.assertEqual(d("偏多", 60, None)[0], "买入")
        self.assertEqual(d("偏空", 40, None)[0], "卖出")

    def test_confidence_bands(self):
        c = TraderAgent._confidence
        self.assertEqual(c(80, 0.9, 6), "高")
        self.assertEqual(c(60, 0.6, 2), "中")
        self.assertEqual(c(40, 0.9, 6), "低")
        self.assertEqual(c(None, 0.9, 6), "低")
        self.assertEqual(c(80, None, 6), "低")

    def test_entry_zone_variants(self):
        z = TraderAgent._entry_zone
        reports = {"趋势 Agent": {"ma5": 29.0, "ma20": 31.0}}
        self.assertIn("29.00-31.00", z("买入", {}, reports))
        self.assertIn("若跌破 MA20 31.00", z("卖出", {}, reports))
        self.assertIn("等待方向确认", z("持有", {}, reports))
        self.assertIn("仅有现价 30.00", z("买入", {"price": 30.0}, {}))
        self.assertIn("行情数据不足", z("买入", {}, {}))

    def test_invalidation_and_thesis(self):
        debate = {
            "judge": {"verdict": "偏空"},
            "bear_case": {"arguments": [{"text": f"空头理由{i}"} for i in range(5)]},
            "bull_case": {"arguments": [{"text": "多头理由"}]},
            "investment_plan": {"thesis": "研究假设", "time_horizon": "中线"},
        }
        sell = TraderAgent().run(debate=debate, composite_score=30, coverage=0.9)
        self.assertEqual(sell["action"], "卖出")
        self.assertEqual(len(sell["invalidation"]), 4)
        self.assertEqual(sell["invalidation"][0], "空头理由0")
        self.assertIn("综合评分回升至 60 以上", sell["invalidation"][-1])
        self.assertIn("主要顾虑：空头理由0", sell["thesis"])

        debate["judge"]["verdict"] = "偏多"
        buy = TraderAgent().run(debate=debate, composite_score=75, coverage=0.9)
        self.assertIn("核心依据：多头理由", buy["thesis"])

    def test_non_dict_inputs_fall_back_to_hold(self):
        result = TraderAgent().run(debate=None, analyst_reports=[], stock="x")
        self.assertEqual(result["action"], "持有")
        self.assertEqual(result["action_code"], "HOLD")
        self.assertEqual(result["confidence"], "低")
        self.assertIn("行情数据不足", result["entry_zone"])
        self.assertIn("暂无可核验的反向信号", result["invalidation"])

    def test_action_code_mapping(self):
        self.assertEqual(TraderAgent.ACTION_CODE,
                         {"买入": "BUY", "持有": "HOLD", "卖出": "SELL"})


class RiskManagementEdgeTest(unittest.TestCase):
    @staticmethod
    def _debate(verdict="偏多"):
        return {"judge": {"verdict": verdict},
                "bull_case": {"arguments": [{"text": "多头理由"}]},
                "bear_case": {"arguments": [{"text": "空头理由"}]}}

    def test_review_rejects_sell_vs_bull_conflict(self):
        result = RiskManagementAgent().run(
            trader={"action": "卖出"}, debate=self._debate("偏多"), coverage=0.9)
        self.assertEqual(result["strategy_review"]["result"], "驳回")
        self.assertTrue(any("偏多" in r for r in result["strategy_review"]["reasons"]))
        self.assertEqual(result["position_size"]["max_pct"], 0)

    def test_conditions_all_triggers(self):
        reports = {
            "风险 Agent": {"annualized_volatility_pct": 70.0, "max_drawdown_20d_pct": -20.0},
            "估值 Agent": {"pe_percentile": 80.0},
            "财务 Agent": {"net_profit_yoy_pct": -10.0},
        }
        result = RiskManagementAgent().run(
            trader={"action": "买入"}, debate=self._debate(), analyst_reports=reports, coverage=0.9)
        text = " ".join(result["final_decision"]["conditions"])
        for needle in ("单笔浮亏达到 8%", "波动率回落至 35%", "PE 历史分位升破 70%",
                       "盈利拐点"):
            self.assertIn(needle, text)

    def test_risk_checks_all_signals(self):
        reports = {
            "风险 Agent": {"annualized_volatility_pct": 40.0, "max_drawdown_20d_pct": -12.0},
            "估值 Agent": {"pe_percentile": 75.0},
            "财务 Agent": {"net_profit_yoy_pct": -40.0},
            "新闻 Agent": {"items": [{"tags": ["风险事项"]}]},
            "流动性 Agent": {"amount": 1e7},
        }
        result = RiskManagementAgent().run(
            trader={"action": "买入"}, debate=self._debate(),
            analyst_reports=reports, coverage=0.3)
        names = {c["name"] for c in result["risk_checks"]}
        self.assertEqual(names, {"波动率", "回撤", "估值分位", "盈利质量",
                                 "事件风险", "数据覆盖", "流动性"})

    def test_position_zero_cap_and_rejected(self):
        p = RiskManagementAgent._position
        self.assertEqual(p("卖出", "高", 1.0, "低")["max_pct"], 0)
        self.assertEqual(p("买入", "高", 1.0, "低")["recommended"], "17-24%")
        self.assertEqual(p("买入", "低", 0.2, "高")["recommended"], "1%")
        rejected = p("买入", "高", 1.0, "低", "驳回")
        self.assertEqual(rejected["recommended"], "0%（策略审核未通过）")
        self.assertEqual(rejected["max_pct"], 0)

    def test_risk_debate_without_arguments(self):
        result = RiskManagementAgent().run()
        debate = result["risk_debate"]
        self.assertEqual(set(debate), {"aggressive", "conservative", "neutral"})
        self.assertIn("多头证据有限", debate["aggressive"])
        self.assertIn("空头证据有限", debate["conservative"])
        self.assertIn("未触发高风险项", debate["conservative"])
        self.assertEqual(result["risk_level"], "低")


class DebateEvidenceEdgeTest(unittest.TestCase):
    def test_declining_fundamentals_are_bear_evidence(self):
        bundle = report_bundle([
            ("2023-12-31", 150, 20, 45, 20),
            ("2024-12-31", 130, 16, 41, 17),
            ("2025-12-31", 110, 11, 36, 13),
        ])
        fin = FinancialAgent().run(bundle, bundle)
        items = {x["id"]: x for x in build_evidence({"财务 Agent": fin})["items"]}
        for item_id in ("F1", "F2", "F3", "F4"):
            self.assertEqual(items[item_id]["direction"], "bear", item_id)
        self.assertEqual(items["F1"]["strength"], "中")
        self.assertEqual(items["F3"]["strength"], "中")

    def test_status_direction_partial_is_neutral(self):
        from ashare_hunter.agents.debate_evidence import _status_direction
        self.assertEqual(_status_direction("是", "强", "中"), ("bull", "强"))
        self.assertEqual(_status_direction("否", "强", "中"), ("bear", "中"))
        self.assertEqual(_status_direction("部分", "强", "中"), ("neutral", "弱"))
        self.assertEqual(_status_direction("未知", "强", "中"), ("unknown", None))

    def test_rows_and_sample_text_helpers(self):
        class BrokenFrame:
            def to_dict(self, *args, **kwargs):
                raise RuntimeError("bad frame")

        self.assertEqual(_rows(None), [])
        self.assertEqual(_rows([{"a": 1}, "x"]), [{"a": 1}])
        self.assertEqual(_rows(("x",)), [])
        self.assertEqual(_rows(BrokenFrame()), [])
        self.assertEqual(_sample_text([]), "")
        self.assertEqual(_sample_text([{"title": "公告", "publish_time": "2026-11-18",
                                        "source": "交易所"}]), "《公告｜2026-11-18｜交易所》")

    def test_macro_negative_judgement_and_sector_list_input(self):
        macro = {"rate": {"judgement": "收紧", "value": "加息", "source": "手动录入",
                          "as_of": "2026-11-18"}}
        m1 = build_evidence({}, {"macro": macro})["by_layer"]["宏观面"][0]
        self.assertEqual(m1["answer"], "否")  # 「利率是否有利于科技股」-> 收紧即否
        self.assertEqual(m1["direction"], "bear")
        self.assertEqual(m1["strength"], "弱")
        self.assertIn("外部录入", m1["source"])

        tech = [{"sector_name": "AI算力", "change_pct": 2.5}]
        self.assertEqual(build_evidence({}, {"sectors": tech})["by_layer"]["宏观面"][1]["direction"],
                         "bull")
        non_tech = [{"sector_name": "白酒", "change_pct": 5.0}]
        self.assertEqual(build_evidence({}, {"sectors": non_tech})["by_layer"]["宏观面"][1]["answer"],
                         "数据不足")

    def test_technical_breakdown_and_dead_cross_are_bear(self):
        frame = pd.DataFrame({
            "close": [100.0] * 59 + [90.0],
            "date": pd.date_range("2026-01-01", periods=60).strftime("%Y-%m-%d"),
        })
        tr, mom = TrendAgent().run(frame), MomentumAgent().run(frame)
        self.assertTrue(tr["ma20_breakdown"])
        self.assertTrue(mom["macd_dead_cross"])
        items = {x["id"]: x for x in
                 build_evidence({"趋势 Agent": tr, "动量 Agent": mom})["by_layer"]["技术面"]}
        self.assertEqual(items["T1"]["direction"], "bear")
        self.assertEqual(items["T2"]["direction"], "bear")
        self.assertEqual(items["T4"]["direction"], "bear")
        self.assertEqual(items["T3"]["answer"], "数据不足")


class ResearchDebateEdgeTest(unittest.TestCase):
    def test_low_roe_creates_bear_argument(self):
        result = ResearchDebateAgent().run(
            {"财务 Agent": {"score": 35, "roe_pct": 5.0, "net_profit_yoy_pct": -10.0}},
            30.0, 0.9)
        self.assertTrue(any("低于 8%" in e["text"] for e in result["bear_case"]["arguments"]))
        self.assertEqual(ResearchDebateAgent._grade(20), "弱")

    def test_balanced_verdict_uses_composite_as_tiebreaker(self):
        v = ResearchDebateAgent._verdict
        self.assertEqual(v(4, 4, 80), ("偏多（弱）", "双方均势"))
        self.assertEqual(v(4, 4, 20), ("偏空（弱）", "双方均势"))
        self.assertEqual(v(4, 4, 50), ("中性", "双方均势"))
        self.assertEqual(v(0, 0, 80), ("证据不足", "无"))
        self.assertEqual(v(10, 0, None), ("偏多", "多头研究员"))

    def test_empty_reports_caveats_cover_macro_gap(self):
        result = ResearchDebateAgent().run({}, None, None)
        self.assertEqual(len(result["unresolved_questions"]), 15)
        self.assertTrue(any("宏观层缺少自动数据源" in c for c in result["caveats"]))
        self.assertTrue(any("项检查数据不足" in c for c in result["caveats"]))


class TradingOrchestratorContextTest(unittest.TestCase):
    @staticmethod
    def _stock():
        return {"code": "600001", "name": "测试股份", "amount": 1e8, "change_pct": 1.2}

    def test_macro_context_reaches_debate_only_when_supplied(self):
        macro = {"rate": {"judgement": "有利", "value": "10Y 利率回落",
                          "source": "手动录入", "as_of": "2026-11-18"}}
        injected = TradingAgentsOrchestrator(FakeProvider()).analyze(self._stock(), macro=macro)
        m1 = injected["trading_agents"]["research"]["evidence_by_layer"]["宏观面"][0]
        self.assertEqual(m1["answer"], "是")
        self.assertIn("外部录入", m1["source"])

        plain = TradingAgentsOrchestrator(FakeProvider()).analyze(self._stock())
        self.assertEqual(plain["trading_agents"]["research"]["evidence_by_layer"]["宏观面"][0]["answer"],
                         "数据不足")

    def test_market_and_sectors_context_is_passed_through(self):
        market = {"market_regime": "偏多", "advance_decline_ratio": 0.66, "avg_change_pct": 1.2}
        sectors = pd.DataFrame([{"sector_name": "AI算力", "change_pct": 3.4}])
        result = TradingAgentsOrchestrator(FakeProvider()).analyze(
            self._stock(), market=market, sectors=sectors)
        self.assertEqual(result["market"], market)
        m3 = result["trading_agents"]["research"]["evidence_by_layer"]["宏观面"][2]
        self.assertEqual(m3["answer"], "是")
        self.assertEqual(m3["direction"], "bull")

    def test_final_recommendation_matches_layers(self):
        result = TradingAgentsOrchestrator(FakeProvider()).analyze(self._stock())
        ta = result["trading_agents"]
        final = ta["final_recommendation"]
        self.assertEqual(final["verdict"], ta["research"]["judge"]["verdict"])
        self.assertEqual(final["action"], ta["trading"]["action"])
        self.assertEqual(final["review"], ta["risk"]["strategy_review"]["result"])
        self.assertEqual(result["overall"]["final_action"], final["action"])
        self.assertEqual(result["overall"]["final_risk_level"], final["risk_level"])
        self.assertIn("score", result["overall"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
