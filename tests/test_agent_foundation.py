"""agents 基础层补齐测试：字段工具、报告/LLM 容错、技术面与风险边界。

覆盖现有 157 项测试未触及的分支：
- fundamental_base 的单位换算、别名匹配、同比回退与阈值打分；
- ReportAgent / ResearchAgent / _llm_summary 的文件输出与 LLM 降级；
- TrendAgent / MomentumAgent / LiquidityAgent / RiskAgent / SectorContextAgent
  的空数据、短序列、双市场口径、各判定分支；
- StockAnalysisOrchestrator 的单源失败容错与 _overall 回退。

全部离线运行，不连接 Futu OpenD，不发真实 LLM 请求。
运行：python -m tests.test_agent_foundation
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LLM_ENABLED", "false")

import numpy as np
import pandas as pd

from config import settings
from ashare_hunter.agents import fundamental_base as fb
from ashare_hunter.agents.report_agent import ReportAgent
from ashare_hunter.agents.research_agent import ResearchAgent
from ashare_hunter.agents.financial_agent import FinancialAgent
from ashare_hunter.agents.stock_agent import StockAgent
from ashare_hunter.agents.technical_agent import TechnicalAgent
from ashare_hunter.agents.stock_analysis import (
    TrendAgent, MomentumAgent, LiquidityAgent, RiskAgent, SectorContextAgent,
    StockAnalysisOrchestrator,
)
from tests.test_agents import FakeProvider


def history(close, volume=None, with_date=True):
    data = {"close": list(close)}
    if with_date:
        data["date"] = pd.date_range("2026-01-01", periods=len(close)).strftime("%Y-%m-%d")
    if volume is not None:
        data["volume"] = list(volume)
    return pd.DataFrame(data)


class FundamentalUtilsTest(unittest.TestCase):
    def test_to_number_rejects_missing_and_non_finite(self):
        for value in (None, True, False, float("nan"), float("inf"), float("-inf")):
            self.assertIsNone(fb.to_number(value), value)

    def test_to_number_rejects_placeholder_text(self):
        for value in ("", "   ", "N/A", "na", "none", "--", "-", "null", "NaN", "abc", "+", "1e309"):
            self.assertIsNone(fb.to_number(value), value)

    def test_to_number_handles_units_separators_and_signs(self):
        self.assertAlmostEqual(fb.to_number("1.5万亿"), 1.5e12)
        self.assertAlmostEqual(fb.to_number("2亿"), 2e8)
        self.assertAlmostEqual(fb.to_number("3万"), 3e4)
        self.assertAlmostEqual(fb.to_number("1,234.5%"), 1234.5)
        self.assertAlmostEqual(fb.to_number("-12.5％"), -12.5)
        self.assertAlmostEqual(fb.to_number(18), 18.0)
        self.assertAlmostEqual(fb.to_number("2.5e3"), 2500.0)

    def test_normalise_name_strips_symbols(self):
        self.assertEqual(fb.normalise_name("Return on Equity (ROE) %"), "returnonequityroe")
        self.assertEqual(fb.normalise_name(None), "")
        self.assertEqual(fb.normalise_name("销售毛利率（%）"), "销售毛利率")

    def test_find_item_exact_match_wins_over_contains(self):
        report = {"item_list": [
            {"display_name": "净利润", "data": 1},
            {"display_name": "归属于母公司股东的净利润", "data": 2},
        ]}
        item = fb.find_item(report, ["归属于母公司股东的净利润", "净利润"])
        self.assertEqual(item["data"], 2)

    def test_find_item_fuzzy_and_empty_inputs(self):
        report = {"item_list": [{"display_name": "销售毛利率", "data": 42.0}]}
        self.assertEqual(fb.find_item(report, ["毛利率"])["data"], 42.0)
        self.assertIsNone(fb.find_item(None, ["毛利率"]))
        self.assertIsNone(fb.find_item(report, ["", "  "]))
        self.assertIsNone(fb.find_item({"item_list": []}, ["毛利率"]))

    def test_pick_metric_skips_empty_period_and_honours_yoy_flag(self):
        reports = [
            {"date_time_str": "2025-12-31", "item_list": [{"display_name": "营业收入", "data": "N/A"}]},
            {"period_text": "2024年报", "financial_type": "ANNUAL",
             "item_list": [{"display_name": "营业收入", "data": 120, "yoy": 20}]},
        ]
        metric = fb.pick_metric(reports, ["营业收入"])
        self.assertEqual(metric["value"], 120.0)
        self.assertEqual(metric["yoy"], 20.0)
        self.assertEqual(metric["period"], "2024年报")
        self.assertEqual(metric["fiscal_type"], "ANNUAL")
        self.assertIsNone(fb.pick_metric(reports, ["营业收入"], use_yoy=False)["yoy"])
        self.assertIsNone(fb.pick_metric([], ["营业收入"]))

    def test_compute_yoy_requires_two_periods_and_nonzero_base(self):
        reports = [
            {"date_time_str": "2025-12-31", "item_list": [{"display_name": "营业收入", "data": 125}]},
            {"date_time_str": "2024-12-31", "item_list": [{"display_name": "营业收入", "data": 100}]},
        ]
        calc = fb.compute_yoy(reports, ["营业收入"])
        self.assertAlmostEqual(calc["yoy"], 25.0)
        self.assertEqual(calc["from_period"], "2024-12-31")
        self.assertIsNone(fb.compute_yoy(reports[:1], ["营业收入"]))
        zero_base = [reports[0], {"date_time_str": "2024-12-31",
                                  "item_list": [{"display_name": "营业收入", "data": 0}]}]
        self.assertIsNone(fb.compute_yoy(zero_base, ["营业收入"]))

    def test_score_by_thresholds_both_directions(self):
        self.assertIsNone(fb.score_by_thresholds(None, [(0, 50)]))
        up = [(80, 90), (60, 70), (0, 40)]
        self.assertEqual(fb.score_by_thresholds(85, up), 90)
        self.assertEqual(fb.score_by_thresholds(65, up), 70)
        # 现有实现语义：降序边界 + higher_is_better=False 会先命中最大边界，
        # 低值反而得低分。此处锁定真实行为；估值 Agent 正是这样调用，
        # 评分方向与注释相反，已作为待修问题在会话中单独报告。
        down = [(2, 35), (1, 60), (0, 85)]
        self.assertEqual(fb.score_by_thresholds(0.5, down, higher_is_better=False), 35)
        self.assertEqual(fb.score_by_thresholds(3, down, higher_is_better=False), 85)
        self.assertEqual(fb.score_by_thresholds(-5, [(0, 50)]), 50)

    def test_score_label_and_formatters(self):
        self.assertEqual(fb.score_label(None), "数据不足")
        self.assertEqual(fb.score_label(80), "强")
        self.assertEqual(fb.score_label(65), "偏强")
        self.assertEqual(fb.score_label(45), "中性")
        self.assertEqual(fb.score_label(30), "偏弱")
        self.assertEqual(fb.score_label(29.9), "弱")
        self.assertEqual(fb.fmt_pct(None), "数据不足")
        self.assertEqual(fb.fmt_pct(12.345, 1), "12.3%")
        self.assertEqual(fb.fmt_num(None), "数据不足")
        self.assertEqual(fb.fmt_num(3.14159, 3), "3.142")


class _FakeResponse:
    def __init__(self, content="模型输出", error=None):
        self.content = content
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class ReportAgentTest(unittest.TestCase):
    @staticmethod
    def _frames():
        sectors = pd.DataFrame([{"sector_name": "半导体", "change_pct": 3.2, "score": 88.0}])
        stocks = pd.DataFrame([{"code": "SH.600519", "name": "贵州茅台", "score": 91.0}])
        market = {
            "market_regime": "偏强", "up_count": 2600, "down_count": 1200,
            "advance_decline_ratio": 0.68, "total_amount_yuan": 1.2e12,
        }
        return market, sectors, stocks

    def test_save_writes_three_files_with_traceable_content(self):
        market, sectors, stocks = self._frames()
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                path = Path(ReportAgent().save(market, sectors, stocks, "研究解读内容", "2026-11-19 09:00"))
                self.assertTrue(path.exists())
                text = path.read_text(encoding="utf-8")
                for needle in ("A-Share Hunter 扫描报告", "2026-11-19 09:00", "偏强",
                               "68.0%", "1,200,000,000,000", "半导体", "贵州茅台",
                               "研究解读内容", "不构成投资建议"):
                    self.assertIn(needle, text)
                out = Path("output")
                sector_files = sorted(out.glob("sectors_*.csv"))
                self.assertEqual(len(sector_files), 1)
                self.assertTrue(sector_files[0].read_bytes().startswith(b"\xef\xbb\xbf"))
                csv_files = sorted(out.glob("stocks_*.csv"))
                self.assertEqual(len(csv_files), 1)
                self.assertTrue(csv_files[0].read_bytes().startswith(b"\xef\xbb\xbf"))
                self.assertEqual(pd.read_csv(csv_files[0], encoding="utf-8-sig").iloc[0]["code"], "SH.600519")
            finally:
                os.chdir(original)

    def test_save_handles_empty_frames(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                path = Path(ReportAgent().save(
                    {"market_regime": "未知"}, pd.DataFrame(), pd.DataFrame(), "", "now"))
                text = path.read_text(encoding="utf-8")
                self.assertIn("无符合条件板块", text)
                self.assertIn("无符合条件个股", text)
            finally:
                os.chdir(original)


class LlmBoundaryTest(unittest.TestCase):
    def test_research_agent_disabled_returns_rule_based_text(self):
        with patch.object(settings, "llm_enabled", False):
            text = ResearchAgent().explain({"market_regime": "偏强"}, pd.DataFrame(), pd.DataFrame())
        self.assertIn("LLM未启用", text)

    def test_research_agent_posts_expected_payload(self):
        with patch.multiple(settings, llm_enabled=True, llm_api_key="test-key",
                            llm_base_url="http://llm.local/v1/", llm_model="test-model"):
            with patch("ashare_hunter.agents.research_agent.requests.post",
                       return_value=_FakeResponse("解读结果")) as post:
                text = ResearchAgent().explain(
                    {"market_regime": "偏强"},
                    pd.DataFrame([{"sector_name": "AI"}]),
                    pd.DataFrame([{"code": "SH.600519"}]),
                )
        self.assertEqual(text, "解读结果")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://llm.local/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(kwargs["json"]["model"], "test-model")
        self.assertEqual(kwargs["timeout"], 60)
        self.assertIn("不得编造", kwargs["json"]["messages"][0]["content"])

    def test_research_agent_failure_degrades_gracefully(self):
        with patch.multiple(settings, llm_enabled=True, llm_api_key="k"):
            with patch("ashare_hunter.agents.research_agent.requests.post",
                       side_effect=RuntimeError("timeout")):
                text = ResearchAgent().explain({}, pd.DataFrame(), pd.DataFrame())
        self.assertIn("LLM解读调用失败", text)
        self.assertIn("量化结果仍可查看", text)

    def test_llm_summary_disabled_success_and_failure(self):
        result = {"stock": {"code": "600519"}, "market": {}, "agents": [], "overall": {}}
        with patch.object(settings, "llm_enabled", False):
            self.assertIn("未启用", StockAnalysisOrchestrator._llm_summary(result))
        with patch.multiple(settings, llm_enabled=True, llm_api_key="k"):
            with patch("ashare_hunter.agents.stock_analysis.requests.post",
                       return_value=_FakeResponse("总结内容")):
                self.assertEqual(StockAnalysisOrchestrator._llm_summary(result), "总结内容")
            with patch("ashare_hunter.agents.stock_analysis.requests.post",
                       side_effect=OSError("network down")):
                self.assertIn("LLM 总结调用失败", StockAnalysisOrchestrator._llm_summary(result))


class TechnicalAgentBoundaryTest(unittest.TestCase):
    def test_trend_missing_and_short_history(self):
        agent = TrendAgent()
        for frame in (None, pd.DataFrame(), pd.DataFrame({"open": [1, 2]})):
            self.assertEqual(agent.run(frame)["status"], "数据不足")
        short = agent.run(history([100.0] * 10))
        self.assertEqual(short["status"], "数据不足")
        self.assertIn("至少需要 20 根", short["conclusion"])

    def test_trend_bearish_alignment(self):
        result = TrendAgent().run(history(np.linspace(120, 80, 60)))
        self.assertEqual(result["trend"], "空头排列")
        self.assertFalse(result["above_ma20"])
        self.assertFalse(result["uptrend"])

    def test_trend_oscillating_strong_and_weak(self):
        strong = TrendAgent().run(history([100.0] * 50 + [110.0] * 5 + [105.0]))
        self.assertEqual(strong["trend"], "震荡偏强")
        self.assertTrue(strong["above_ma20"])
        weak = TrendAgent().run(history([100.0] * 55 + [120.0] * 4 + [90.0]))
        self.assertEqual(weak["trend"], "震荡偏弱")
        self.assertFalse(weak["above_ma20"])

    def test_trend_without_date_has_no_as_of(self):
        result = TrendAgent().run(history([100.0 + i * 0.1 for i in range(40)], with_date=False))
        self.assertEqual(result["status"], "完成")
        self.assertIsNone(result["as_of"])

    def test_momentum_missing_and_short_history(self):
        agent = MomentumAgent()
        for frame in (None, pd.DataFrame(), pd.DataFrame({"open": [1, 2]})):
            self.assertEqual(agent.run(frame)["status"], "数据不足")
        short = agent.run(history([100.0] * 14))
        self.assertEqual(short["status"], "数据不足")
        self.assertIn("K 线不足", short["conclusion"])

    def test_momentum_rsi_branches(self):
        agent = MomentumAgent()
        # 单边上涨 / 横盘没有下跌日，rolling loss 为 0，RSI 无法计算，必须标数据不足
        for seq in ([100.0] * 60, np.linspace(100, 200, 60)):
            result = agent.run(history(seq))
            self.assertEqual(result["momentum"], "指标不足")
            self.assertIsNone(result["rsi14"])
        n = np.arange(60)
        up = 100 + n * 0.5 + np.where(n % 2 == 0, 0.0, -1.0)
        down = 200 - n * 0.5 + np.where(n % 2 == 0, 0.0, 1.0)
        self.assertEqual(agent.run(history(up))["momentum"], "短线偏热")
        self.assertEqual(agent.run(history(down))["momentum"], "动量偏弱")
        self.assertEqual(agent.run(history(np.linspace(200, 100, 60)))["momentum"], "动量偏弱")
        self.assertEqual(agent.run(history([100.0, 101.0] * 30))["momentum"], "中性")

    def test_momentum_volume_branches(self):
        agent = MomentumAgent()
        self.assertEqual(agent.run(history([100.0] * 60, [1000.0] * 60))["volume_status"], "持平")
        shrink = agent.run(history([100.0] * 60, [1000.0] * 55 + [500.0] * 5))
        self.assertEqual(shrink["volume_status"], "萎缩")
        self.assertLess(shrink["volume_expansion"], 0.8)
        zero = agent.run(history([100.0] * 60, [0.0] * 60))
        self.assertEqual(zero["volume_status"], "数据不足")
        self.assertIsNone(zero["volume_expansion"])
        short_vol = history([100.0] * 60, [1000.0] * 10 + [float("nan")] * 50)
        self.assertEqual(agent.run(short_vol)["volume_status"], "数据不足")

    def test_momentum_cross_signals_and_dates(self):
        late_crash = [100 + i * 0.5 for i in range(55)] + [127 - i * 8 for i in range(5)]
        crash = MomentumAgent().run(history(late_crash))
        self.assertTrue(crash["macd_dead_cross"])
        self.assertFalse(crash["macd_golden_cross"])
        self.assertEqual(crash["macd_cross_window"], 5)
        self.assertTrue(crash["macd_cross_date"])

        late_rally = [150 - i * 0.5 for i in range(55)] + [123 + i * 8 for i in range(5)]
        rally = MomentumAgent().run(history(late_rally, with_date=False))
        self.assertTrue(rally["macd_golden_cross"])
        self.assertFalse(rally["macd_dead_cross"])
        self.assertIsNone(rally["macd_cross_date"])
        self.assertIsNone(rally["as_of"])

    def test_liquidity_full_missing_and_usd_unit(self):
        full = LiquidityAgent().run({"amount": 5e8, "turnover": 1.2, "volume_ratio": 1.5})
        self.assertIn("亿元", full["conclusion"])
        self.assertIn("换手率字段 1.20", full["conclusion"])
        self.assertAlmostEqual(full["amount"], 5e8)
        missing = LiquidityAgent().run({})
        self.assertIn("成交额未知", missing["conclusion"])
        self.assertIsNone(missing["amount"])
        self.assertEqual(missing["status"], "完成")
        usd = LiquidityAgent().run({"market": "US", "amount": 5e8})
        self.assertIn("亿美元", usd["conclusion"])
        self.assertIn("亿美元", LiquidityAgent().run({"currency": "USD", "amount": 5e8})["conclusion"])

    def test_risk_price_flags_and_missing_history(self):
        up = RiskAgent().run({"change_pct": 8.0}, pd.DataFrame())
        self.assertTrue(any("追高" in f for f in up["risk_flags"]))
        down = RiskAgent().run({"change_pct": -6.0}, pd.DataFrame())
        self.assertTrue(any("跌幅较大" in f for f in down["risk_flags"]))
        empty = RiskAgent().run({}, pd.DataFrame())
        self.assertTrue(any("历史数据不足" in f for f in empty["risk_flags"]))
        self.assertIsNone(empty["annualized_volatility_pct"])
        self.assertIsNone(empty["max_drawdown_20d_pct"])

    def test_risk_drawdown_and_clean_case(self):
        crash = RiskAgent().run({"change_pct": 0.0},
                                history([100.0 + i for i in range(35)] + [80.0] * 5))
        self.assertTrue(any("最大回撤超过 10%" in f for f in crash["risk_flags"]))
        clean = RiskAgent().run({"change_pct": 0.0}, history(np.linspace(100, 110, 60)))
        self.assertTrue(any("未触发简单风险阈值" in f for f in clean["risk_flags"]))

    def test_sector_context_matches_and_empty_cases(self):
        sectors = pd.DataFrame([
            {"sector_name": "半导体", "change_pct": 3.2, "score": 88.0, "breadth": 0.7},
            {"sector_name": "", "change_pct": 9.9, "score": 99.0, "breadth": 1.0},
        ])
        stock = {"sector_names": "半导体,人工智能"}
        matched = SectorContextAgent().run(stock, sectors)
        self.assertEqual(len(matched["matched_sectors"]), 1)
        self.assertIn("半导体", matched["conclusion"])
        self.assertAlmostEqual(matched["matched_sectors"][0]["change_pct"], 3.2)
        for frame, payload in ((pd.DataFrame(), stock), (sectors, {}), (None, stock)):
            self.assertEqual(SectorContextAgent().run(payload, frame)["matched_sectors"], [])


class OrchestratorBoundaryTest(unittest.TestCase):
    def test_safe_fetch_returns_traceable_unavailable_marker(self):
        def broken():
            raise RuntimeError("接口挂了")

        result = StockAnalysisOrchestrator._safe_fetch("测试源", broken)
        self.assertFalse(result["available"])
        self.assertIn("测试源 调用失败", result["reason"])
        self.assertIn("接口挂了", result["reason"])

    def test_overall_fallback_labels(self):
        def agents(trend, momentum, flags=None):
            return [
                {"agent": "趋势 Agent", "trend": trend},
                {"agent": "动量 Agent", "momentum": momentum},
                {"agent": "风险 Agent", "risk_flags": flags or []},
            ]

        self.assertEqual(StockAnalysisOrchestrator._overall(agents("多头排列", "动量偏强"))["label"],
                         "技术面偏强")
        self.assertEqual(StockAnalysisOrchestrator._overall(agents("空头排列", "中性"))["label"],
                         "技术面偏弱")
        self.assertEqual(StockAnalysisOrchestrator._overall(agents("震荡偏强", "中性"))["label"],
                         "技术面中性 / 需观察")
        flagged = agents("多头排列", "动量偏强", ["近20日最大回撤超过 10%"])
        self.assertEqual(StockAnalysisOrchestrator._overall(flagged)["label"], "技术面中性 / 需观察")

    def test_broken_provider_does_not_abort_pipeline(self):
        class BrokenProvider(FakeProvider):
            def get_daily_history(self, code, days=80):
                raise RuntimeError("K线接口异常")

            def get_financials(self, code, statement_type=4, financial_type="ANNUAL", num=5):
                raise RuntimeError("财报接口异常")

            def get_single_snapshot(self, code):
                raise RuntimeError("快照接口异常")

        result = StockAnalysisOrchestrator(BrokenProvider()).analyze({"code": "600519", "name": "测试"})
        self.assertEqual(len(result["agents"]), 10)
        self.assertEqual(result["history_rows"], 0)
        self.assertEqual(result["history_latest_date"], "未知")
        self.assertIn("财报接口异常", result["data_sources"]["key_indicators"]["reason"])
        self.assertIn("快照接口异常", result["data_sources"]["snapshot"]["reason"])
        self.assertIn("overall", result)
        self.assertIn("llm_summary", result)

    def test_non_dataframe_history_falls_back_to_empty(self):
        class WeirdHistoryProvider(FakeProvider):
            def get_daily_history(self, code, days=80):
                return {"close": [1, 2, 3]}

        result = StockAnalysisOrchestrator(WeirdHistoryProvider()).analyze(
            {"code": "600519", "name": "测试"})
        self.assertEqual(result["history_rows"], 0)
        by_name = {a["agent"]: a for a in result["agents"]}
        self.assertEqual(by_name["趋势 Agent"]["status"], "数据不足")
        self.assertEqual(by_name["动量 Agent"]["status"], "数据不足")

    def test_us_stock_uses_dollar_amount_unit(self):
        stock = {"code": "AAPL", "name": "Apple", "market": "US",
                 "currency": "USD", "amount": 5e8, "change_pct": 1.2}
        result = StockAnalysisOrchestrator(FakeProvider()).analyze(stock)
        by_name = {a["agent"]: a for a in result["agents"]}
        self.assertIn("亿美元", by_name["流动性 Agent"]["conclusion"])
        for key in ("key_indicators", "income_statement", "snapshot", "pe_detail",
                    "pb_detail", "capital_flow", "distribution", "institutional"):
            self.assertIn(key, result["data_sources"])


class FundamentalExtraBoundaryTest(unittest.TestCase):
    def test_to_number_value_error_branch(self):
        # cleaned 非空但 float() 解析失败，必须走 except 返回 None
        self.assertIsNone(fb.to_number("1e"))
        self.assertIsNone(fb.to_number("1e2e3"))

    def test_compute_yoy_skips_periods_without_matching_field(self):
        reports = [
            {"date_time_str": "2025-12-31", "item_list": []},
            {"date_time_str": "2024-12-31",
             "item_list": [{"display_name": "营业收入", "data": "N/A"}]},
            {"date_time_str": "2023-12-31",
             "item_list": [{"display_name": "营业收入", "data": 100}]},
            {"date_time_str": "2022-12-31",
             "item_list": [{"display_name": "营业收入", "data": 80}]},
        ]
        calc = fb.compute_yoy(reports, ["营业收入"])
        self.assertAlmostEqual(calc["yoy"], 25.0)
        self.assertEqual(calc["from_period"], "2022-12-31")

    def test_financial_agent_skips_bad_values_and_discloses_missing_fields(self):
        bundle = {"available": True, "reports": [
            {"date_time_str": "2025-12-31", "item_list": [
                {"display_name": "净资产收益率", "data": "N/A"},
                {"display_name": "营业总收入", "data": 120}]},
            {"date_time_str": "2024-12-31", "item_list": [
                {"display_name": "净资产收益率", "data": 16},
                {"display_name": "营业总收入", "data": 100}]},
        ]}
        result = FinancialAgent().run(bundle, bundle)
        self.assertEqual(result["growth_trend"]["roe"]["status"], "数据不足")
        self.assertIn("未取得字段", result["conclusion"])
        self.assertIn("gross_margin", result["conclusion"])


class WorkflowNodeBoundaryTest(unittest.TestCase):
    def test_stock_agent_skips_failed_sector_with_warning(self):
        class FailingProvider:
            def get_sector_stocks(self, name, kind, market="SH"):
                raise RuntimeError("板块权限不足")

        sectors = pd.DataFrame([{"sector_name": "半导体", "board_type": "industry", "score": 88.0}])
        with self.assertLogs("ashare_hunter.agents.stock_agent", level="WARNING") as logs:
            result = StockAgent().run(sectors, FailingProvider(), market="SH")
        self.assertTrue(result.empty)
        self.assertIn("板块权限不足", " ".join(logs.output))

    def test_stock_agent_fills_missing_columns_and_filters(self):
        class Provider:
            def get_sector_stocks(self, name, kind, market="SH"):
                return pd.DataFrame([
                    {"code": "SH.600001", "change_pct": 2.0, "amount": 1e8,
                     "turnover": 1.2, "volume_ratio": 1.4},
                    {"code": "SH.600002", "name": "ST风险", "change_pct": 3.0,
                     "amount": 1e8, "turnover": 1.2, "volume_ratio": 1.4},
                    {"code": "SH.600003", "name": "小额", "change_pct": 1.0,
                     "amount": 1e6, "turnover": 1.2, "volume_ratio": 1.4},
                ])

        sectors = pd.DataFrame([{"sector_name": "半导体", "board_type": "industry", "score": 88.0}])
        result = StockAgent().run(sectors, Provider(), min_amount=5e7)
        self.assertEqual(list(result["code"]), ["SH.600001"])
        self.assertIn("name", result.columns)
        for col in ("sector_names", "return_score", "amount_score", "score"):
            self.assertIn(col, result.columns)

    def test_technical_agent_marks_history_exception_and_empty_input(self):
        class FailingProvider:
            def get_daily_history(self, code):
                raise RuntimeError("日线接口超时")

        provider = FailingProvider()
        stocks = pd.DataFrame([{"code": "SH.600001", "name": "测试"}])
        result = TechnicalAgent().enrich(stocks, provider)
        self.assertIn("日线接口超时", result.iloc[0]["history_status"])
        self.assertTrue(TechnicalAgent().enrich(pd.DataFrame(), provider).empty)

    def test_technical_agent_computes_real_moving_averages(self):
        class Provider:
            def get_daily_history(self, code):
                return pd.DataFrame({"close": [float(100 + i) for i in range(30)]})

        result = TechnicalAgent().enrich(pd.DataFrame([{"code": "SH.600001"}]), Provider())
        row = result.iloc[0]
        self.assertAlmostEqual(row["ma5"], 127.0)
        self.assertAlmostEqual(row["ma20"], 119.5)
        self.assertTrue(row["above_ma20"])
        self.assertGreater(row["return_5d_pct"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
