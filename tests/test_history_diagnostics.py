"""日线数据可观测性回归：失败原因必须穿透数据源、容错层、结果、CLI 与 GUI。

运行：python -m unittest tests.test_history_diagnostics -v
全程离线，不连接 Futu OpenD，不发起 LLM 请求。
"""

import contextlib
import io
import logging
import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LLM_ENABLED", "false")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import futu as ft

import main as cli
from ashare_hunter.agents.stock_analysis import StockAnalysisOrchestrator, TrendAgent
from ashare_hunter.agents.trading_agents_orchestrator import TradingAgentsOrchestrator
from ashare_hunter.data.debug_fixture import (
    DebugFixtureProvider, fixture_market, fixture_sectors, fixture_stock,
)
from ashare_hunter.data.futu_provider import FutuProvider
from gui import StockAnalysisDialog

PROVIDER_LOGGER = "ashare_hunter.data.futu_provider"
AGENT_LOGGER = "ashare_hunter.agents.stock_analysis"

# 诊断分支会主动打 warning；避免在无日志配置的测试进程里落到 stderr 噪音。
# assertLogs 会临时替换 handlers，因此不影响下面的日志断言。
logging.getLogger(AGENT_LOGGER).addHandler(logging.NullHandler())


def _history(rows=25, *, close=None, with_close=True, with_date=True):
    data = {}
    if with_date:
        data["date"] = [f"2026-01-{i + 1:02d}" for i in range(rows)]
    if with_close:
        data["close"] = close if close is not None else [100.0 + i for i in range(rows)]
    return pd.DataFrame(data)


class _BoomProvider(DebugFixtureProvider):
    """日线接口抛异常，其余数据源保持正常。"""

    def get_daily_history(self, code, days=80):
        raise RuntimeError("Futu 历史 K 线获取失败 SH.600519：无历史K线行情权限")


class _FakeCtx:
    def __init__(self, ret=ft.RET_OK, data=None, page_key=None, exc=None):
        self.ret, self.data, self.page_key, self.exc = ret, data, page_key, exc
        self.calls = 0

    def request_history_kline(self, *args, **kwargs):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.ret, self.data, self.page_key


class TestHistoryDiagnosis(unittest.TestCase):
    """_history_diagnosis 与 TrendAgent 使用同一口径：有效 close >= 20 根。"""

    def diagnose(self, raw):
        return StockAnalysisOrchestrator._history_diagnosis(raw)[1]

    def test_normal_frame_available(self):
        diag = self.diagnose(_history(30))
        self.assertTrue(diag["available"])
        self.assertEqual(diag["rows"], 30)
        self.assertEqual(diag["valid_close_rows"], 30)
        self.assertEqual(diag["latest_date"], "2026-01-30")
        self.assertIsNone(diag["reason"])
        self.assertIn("close", diag["columns"])

    def test_exactly_20_valid_rows_available(self):
        self.assertTrue(self.diagnose(_history(20))["available"])

    def test_19_rows_unavailable(self):
        diag = self.diagnose(_history(19))
        self.assertFalse(diag["available"])
        self.assertIn("19 根", diag["reason"])
        self.assertIn("至少需要 20 根", diag["reason"])

    def test_empty_frame_unavailable(self):
        diag = self.diagnose(pd.DataFrame(columns=["date", "close"]))
        self.assertFalse(diag["available"])
        self.assertEqual(diag["rows"], 0)
        self.assertIn("空表", diag["reason"])
        self.assertIn("行情权限", diag["reason"])

    def test_missing_close_column(self):
        diag = self.diagnose(pd.DataFrame({"date": ["2026-01-01"] * 25}))
        self.assertFalse(diag["available"])
        self.assertEqual(diag["valid_close_rows"], 0)
        self.assertIn("close", diag["reason"])

    def test_non_numeric_close(self):
        diag = self.diagnose(_history(25, close=["N/A"] * 25))
        self.assertFalse(diag["available"])
        self.assertEqual(diag["valid_close_rows"], 0)
        self.assertIn("0 根", diag["reason"])

    def test_nan_tail_still_available(self):
        diag = self.diagnose(_history(25, close=[100.0] * 20 + [None] * 5))
        self.assertTrue(diag["available"])
        self.assertEqual(diag["valid_close_rows"], 20)

    def test_error_dict_preserves_reason(self):
        raw = {"available": False, "reason": "历史K线 调用失败：行情权限不足"}
        frame, diag = StockAnalysisOrchestrator._history_diagnosis(raw)
        self.assertFalse(diag["available"])
        self.assertIn("行情权限不足", diag["reason"])
        self.assertTrue(frame.empty)
        self.assertIn("close", frame.columns)

    def test_none_result_reports_type(self):
        diag = self.diagnose(None)
        self.assertFalse(diag["available"])
        self.assertIn("未返回 DataFrame", diag["reason"])
        self.assertIn("NoneType", diag["reason"])

    def test_diagnosis_matches_trend_agent(self):
        for rows in (0, 19, 20, 25):
            frame = _history(rows)
            diag = self.diagnose(frame)
            status = TrendAgent().run(frame)["status"]
            with self.subTest(rows=rows):
                self.assertEqual(diag["available"], status == "完成")


class TestSafeFetchLogging(unittest.TestCase):
    def test_exception_logs_warning_and_keeps_reason(self):
        def boom():
            raise ValueError("接口炸了")

        with self.assertLogs(AGENT_LOGGER, level="WARNING") as logs:
            result = StockAnalysisOrchestrator._safe_fetch("历史K线", boom)
        self.assertFalse(result["available"])
        self.assertIn("接口炸了", result["reason"])
        text = "\n".join(logs.output)
        self.assertIn("历史K线", text)
        self.assertIn("ValueError", text)
        self.assertIn("已降级", text)

    def test_success_returns_value_unchanged(self):
        frame = _history()
        self.assertIs(StockAnalysisOrchestrator._safe_fetch("历史K线", lambda: frame), frame)


class TestOrchestratorDiagnostics(unittest.TestCase):
    def analyze(self, provider, scenario="bull"):
        return StockAnalysisOrchestrator(provider).analyze(
            fixture_stock("SH.600519", "贵州茅台", "A", scenario)
        )

    def test_bull_history_available(self):
        diag = self.analyze(DebugFixtureProvider(scenario="bull"))["data_sources"]["history"]
        self.assertTrue(diag["available"])
        self.assertEqual(diag["rows"], 90)
        self.assertIsNone(diag["reason"])

    def test_sparse_reason_in_result(self):
        result = self.analyze(DebugFixtureProvider(scenario="sparse"), "sparse")
        diag = result["data_sources"]["history"]
        self.assertFalse(diag["available"])
        self.assertIn("空表", diag["reason"])
        by_name = {a["agent"]: a["status"] for a in result["agents"]}
        self.assertEqual(by_name["趋势 Agent"], "数据不足")
        self.assertEqual(by_name["动量 Agent"], "数据不足")

    def test_exception_reason_not_swallowed(self):
        result = self.analyze(_BoomProvider(scenario="bull"))
        diag = result["data_sources"]["history"]
        self.assertFalse(diag["available"])
        self.assertIn("无历史K线行情权限", diag["reason"])

    def test_top_level_fields_still_compatible(self):
        result = self.analyze(DebugFixtureProvider(scenario="bull"))
        self.assertEqual(result["history_rows"], 90)
        self.assertNotEqual(result["history_latest_date"], "未知")
        self.assertIn("key_indicators", result["data_sources"])


class TestProviderLogging(unittest.TestCase):
    def setUp(self):
        self.provider = FutuProvider(request_interval=0)

    def run_history(self, ctx, level):
        with patch.object(self.provider, "_ctx", return_value=ctx):
            with self.assertLogs(PROVIDER_LOGGER, level=level) as logs:
                result = self.provider.get_daily_history("SH.600519", days=80)
        return result, "\n".join(logs.output)

    def test_ret_error_logged_with_detail(self):
        ctx = _FakeCtx(ret=ft.RET_ERROR, data="无历史K线行情权限")
        with patch.object(self.provider, "_ctx", return_value=ctx):
            with self.assertLogs(PROVIDER_LOGGER, level="ERROR") as logs:
                with self.assertRaises(RuntimeError):
                    self.provider.get_daily_history("SH.600519", days=80)
        text = "\n".join(logs.output)
        self.assertIn("无历史K线行情权限", text)
        self.assertIn("SH.600519", text)
        self.assertIn("返回失败", text)

    def test_request_exception_logged(self):
        ctx = _FakeCtx(exc=RuntimeError("OpenD 未启动"))
        with patch.object(self.provider, "_ctx", return_value=ctx):
            with self.assertLogs(PROVIDER_LOGGER, level="ERROR") as logs:
                with self.assertRaises(RuntimeError):
                    self.provider.get_daily_history("SH.600519", days=80)
        text = "\n".join(logs.output)
        self.assertIn("请求异常", text)
        self.assertIn("OpenD 未启动", text)

    def test_empty_data_warns_about_downstream(self):
        result, text = self.run_history(_FakeCtx(data=pd.DataFrame()), "WARNING")
        self.assertTrue(result.empty)
        self.assertIn("close", result.columns)
        self.assertIn("返回空数据", text)
        self.assertIn("数据不足", text)

    def test_short_valid_close_warns(self):
        frame = pd.DataFrame({
            "time_key": [f"2026-01-{i + 1:02d}" for i in range(19)],
            "close": [100.0 + i for i in range(19)],
        })
        result, text = self.run_history(_FakeCtx(data=frame), "WARNING")
        self.assertEqual(len(result), 19)
        self.assertIn("有效收盘价仅 19 根", text)

    def test_missing_time_key_logs_actual_columns(self):
        frame = pd.DataFrame({"close": [100.0 + i for i in range(25)]})
        ctx = _FakeCtx(data=frame)
        with patch.object(self.provider, "_ctx", return_value=ctx):
            with self.assertLogs(PROVIDER_LOGGER, level="ERROR") as logs:
                with self.assertRaises(KeyError):
                    self.provider.get_daily_history("SH.600519", days=80)
        text = "\n".join(logs.output)
        self.assertIn("字段解析失败", text)
        self.assertIn("实际列", text)
        self.assertIn("close", text)

    def test_normal_history_logs_info(self):
        frame = pd.DataFrame({
            "time_key": [f"2026-01-{i + 1:02d}" for i in range(25)],
            "close": [100.0 + i for i in range(25)],
            "change_rate": [0.5] * 25,
        })
        result, text = self.run_history(_FakeCtx(data=frame), "INFO")
        self.assertEqual(len(result), 25)
        self.assertIn("date", result.columns)
        self.assertIn("有效收盘", text)
        self.assertIn("2026-01-25", text)

    def test_paging_failure_logged(self):
        first = pd.DataFrame({
            "time_key": [f"2026-01-{i + 1:02d}" for i in range(25)],
            "close": [100.0 + i for i in range(25)],
        })
        calls = []

        class _PagingCtx(_FakeCtx):
            def request_history_kline(self, *args, **kwargs):
                calls.append(kwargs.get("page_req_key"))
                if len(calls) == 1:
                    return ft.RET_OK, first, "NEXT"
                return ft.RET_ERROR, "分页限频", None

        with patch.object(self.provider, "_ctx", return_value=_PagingCtx()):
            with self.assertLogs(PROVIDER_LOGGER, level="ERROR") as logs:
                with self.assertRaises(RuntimeError):
                    self.provider.get_daily_history("SH.600519", days=80)
        self.assertIn("分页失败", "\n".join(logs.output))
        self.assertIn("分页限频", "\n".join(logs.output))


class TestCliAndGuiOutput(unittest.TestCase):
    def setUp(self):
        self._patches = [
            patch.object(cli, "setup_logging", return_value=None),
            patch.object(cli, "shutdown_logging", return_value=None),
        ]
        for item in self._patches:
            item.start()
            self.addCleanup(item.stop)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def run_cli(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(argv)
        return code, buf.getvalue()

    def four_layer(self, scenario):
        return TradingAgentsOrchestrator(DebugFixtureProvider(scenario=scenario)).analyze(
            fixture_stock("SH.600519", "贵州茅台", "A", scenario),
            fixture_market("A", scenario), fixture_sectors("A", scenario),
        )

    def test_cli_sparse_prints_reason(self):
        code, out = self.run_cli([
            "analyze", "--code", "SH.600519", "--offline",
            "--fixture", "sparse", "--no-save",
        ])
        self.assertEqual(code, 0)
        self.assertIn("[日线数据] 不可用", out)
        self.assertIn("行情权限", out)
        self.assertIn("趋势、动量 Agent", out)

    def test_cli_detail_prints_available(self):
        code, out = self.run_cli([
            "analyze", "--code", "SH.600519", "--offline",
            "--no-save", "--detail",
        ])
        self.assertEqual(code, 0)
        self.assertIn("[日线数据] 可用", out)
        self.assertIn("90 根有效收盘", out)

    def test_gui_shows_reason_in_overview_and_analysts(self):
        result = self.four_layer("sparse")
        overview = StockAnalysisDialog._overview(result)
        analysts = StockAnalysisDialog._analysts(result)
        self.assertIn("日线数据状态：不可用", overview)
        self.assertIn("行情权限", overview)
        self.assertIn("日线数据不可用", analysts)
        self.assertIn("排查方向", analysts)
        self.assertIn("至少需要 20 根", analysts)

    def test_gui_bull_has_no_warning_block(self):
        result = self.four_layer("bull")
        self.assertIn("日线数据状态：可用", StockAnalysisDialog._overview(result))
        self.assertNotIn("日线数据不可用", StockAnalysisDialog._analysts(result))


if __name__ == "__main__":
    unittest.main(verbosity=2)
