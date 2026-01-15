"""main.py CLI 调试入口的离线测试：不连 Futu OpenD、不发 LLM 请求。

运行：python -m unittest tests.test_main_cli -v
"""

import contextlib
import io
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LLM_ENABLED", "false")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import main as cli
from ashare_hunter.data.debug_fixture import DebugFixtureProvider
from config import settings


class _FakeGraph:
    def __init__(self, result, record=None):
        self.result = result
        self.record = record if record is not None else {}

    def invoke(self, state):
        return self.result


class _FakeUSOrchestrator:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def scan(self, message=None):
        return self.kwargs.pop("_result")


def _scan_result(code="SH.600519", sector="自定义板块", regime="自定义偏强"):
    return {
        "scan_time": "2026-11-19T10:00:00+08:00",
        "market": "A",
        "market_summary": {"market_regime": regime, "up_count": 100, "down_count": 20},
        "top_sectors": pd.DataFrame([{
            "sector_name": sector, "board_type": "concept", "score": 88.0,
            "change_pct": 3.0, "breadth": 0.8,
        }]),
        "top_stocks": pd.DataFrame([{
            "code": code, "name": "调试样本", "sector_names": sector,
            "change_pct": 2.0, "amount": 1.2e9, "score": 80.0,
        }]),
        "market_cap_filter": {
            "enabled": True, "threshold": 5e9, "before": 100, "after": 80,
            "below": 20, "has_field": True,
        },
        "report_path": "output/report.md",
        "explanation": "离线测试扫描结果",
    }


def _fake_workflow_module(result, record):
    module = types.ModuleType("ashare_hunter.graph.workflow")

    def build_workflow(**kwargs):
        record.update(kwargs)
        return _FakeGraph(result)

    module.build_workflow = build_workflow
    return module


class CliTestBase(unittest.TestCase):
    """把日志初始化与 stdout 收拢，避免测试刷屏或留下日志文件。"""

    def setUp(self):
        self._patches = [
            patch.object(cli, "setup_logging", return_value=None),
            patch.object(cli, "shutdown_logging", return_value=None),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        # 屏蔽 root logger 输出：异常路径的堆栈由 CLI 自己 print 到 stdout 断言
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def run_cli(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def analyze_json(self, argv):
        """在临时目录跑 analyze，返回 (退出码, stdout, 结果 dict)。"""
        path = self.tmp_path / "result.json"
        code, out = self.run_cli(list(argv) + ["--json-out", str(path)])
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return code, out, data


class ParserTest(unittest.TestCase):
    def test_no_args_keeps_legacy_behaviour(self):
        args = cli.parse_args([])
        self.assertEqual(args.command, "scan")
        self.assertEqual(args.market, cli.MARKET_A)
        self.assertFalse(args.no_save)
        self.assertIsNone(args.analyze)

    def test_bare_options_are_routed_to_scan(self):
        args = cli.parse_args(["--market", "US", "--min-cap", "20"])
        self.assertEqual(args.command, "scan")
        self.assertEqual(args.market, "US")
        self.assertEqual(args.min_cap, 20)

    def test_analyze_requires_code(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.parse_args(["analyze"])

    def test_analyze_defaults(self):
        args = cli.parse_args(["analyze", "--code", "US.AAPL", "--offline"])
        self.assertEqual(args.command, "analyze")
        self.assertTrue(args.offline)
        self.assertEqual(args.fixture, "bull")
        self.assertEqual(args.market, "A")

    def test_code_matches_variants(self):
        self.assertTrue(cli._code_matches("600519", "SH.600519"))
        self.assertTrue(cli._code_matches("sh.600519", "SH.600519"))
        self.assertTrue(cli._code_matches("US.AAPL", "AAPL"))
        self.assertFalse(cli._code_matches("SH.600519", "SZ.000001"))

    def test_load_json_arg_supports_inline_and_at_file(self):
        self.assertEqual(cli._load_json_arg('{"a": 1}', "t"), {"a": 1})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "macro.json"
            path.write_text('{"rate": {"judgement": "有利"}}', encoding="utf-8")
            self.assertEqual(cli._load_json_arg("@" + str(path), "t")["rate"]["judgement"], "有利")
            self.assertEqual(cli._load_json_arg(str(path), "t")["rate"]["judgement"], "有利")
        with self.assertRaises(ValueError):
            cli._load_json_arg("{not json", "t")


class OfflineAnalysisTest(CliTestBase):
    def test_bull_fixture_prints_four_layers_and_exports_json(self):
        code, out, data = self.analyze_json([
            "analyze", "--code", "SH.600519", "--offline", "--no-save",
        ])
        self.assertEqual(code, 0, out)
        for marker in ("TradingAgents 四层研究", "[最终建议]", "第一层 · 分析师团队",
                       "第二层 · 多空研究辩论", "第三层 · Trader", "第四层 · 风险管理",
                       "数据边界", "完整结果:"):
            self.assertIn(marker, out)
        self.assertIn("离线 fixture=bull", out)
        ta = data["trading_agents"]
        self.assertEqual(ta["final_recommendation"]["action"], "买入")
        self.assertEqual(ta["final_recommendation"]["verdict"], "偏多")
        self.assertIn("sentiment", ta)
        self.assertIn("evidence_matrix", ta["research"])
        self.assertEqual(data["history_rows"], 90)
        self.assertIn("final_action", data["overall"])
        self.assertIn("LLM 总结未启用", data["llm_summary"])

    def test_bear_fixture_flips_verdict(self):
        code, out, data = self.analyze_json([
            "analyze", "--code", "SZ.000001", "--offline", "--fixture", "bear", "--no-save",
        ])
        self.assertEqual(code, 0, out)
        self.assertEqual(data["trading_agents"]["final_recommendation"]["action"], "卖出")
        self.assertEqual(data["trading_agents"]["research"]["judge"]["verdict"], "偏空")

    def test_sparse_fixture_marks_missing_data(self):
        code, out, data = self.analyze_json([
            "analyze", "--code", "SH.600519", "--offline", "--fixture", "sparse", "--no-save",
        ])
        self.assertEqual(code, 0, out)
        self.assertIn("尚缺数据", out)
        cov = data["trading_agents"]["research"]["evidence_coverage"]
        self.assertLessEqual(cov["answered"], 3)
        self.assertEqual(data["history_rows"], 0)
        self.assertIsNone(data["overall"].get("score"))
        self.assertEqual(data["trading_agents"]["final_recommendation"]["action"], "持有")

    def test_macro_json_injection_is_recorded_as_external(self):
        macro = {
            "rate": {"judgement": "有利", "source": "调试录入", "as_of": "2026-11-19"},
            "industry": {"judgement": "提升", "source": "调试录入", "as_of": "2026-11-19"},
        }
        path = self.tmp_path / "macro.json"
        path.write_text(json.dumps(macro, ensure_ascii=False), encoding="utf-8")
        code, out, data = self.analyze_json([
            "analyze", "--code", "SH.600519", "--offline", "--fixture", "sparse",
            "--macro-json", "@" + str(path), "--no-save",
        ])
        self.assertEqual(code, 0, out)
        rows = data["trading_agents"]["research"]["evidence_by_layer"]["宏观面"]
        self.assertTrue(any("外部录入" in str(r.get("source")) for r in rows))
        self.assertGreaterEqual(
            data["trading_agents"]["research"]["evidence_coverage"]["by_layer"]["宏观面"]["answered"], 2
        )

    def test_bad_macro_json_returns_exit_code_2(self):
        code, out = self.run_cli([
            "analyze", "--code", "X", "--offline", "--macro-json", "{broken",
        ])
        self.assertEqual(code, 2)
        self.assertIn("参数错误", out)

    def test_market_and_sector_context_injection(self):
        args = cli.parse_args([
            "analyze", "--code", "SH.600519", "--offline", "--no-save",
            "--market-json", '{"market_regime": "手工偏强"}',
            "--sector", "手工板块",
        ])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = cli.analyze_stock(args)
        self.assertEqual(result["market"]["market_regime"], "手工偏强")
        sector_agent = next(a for a in result["agents"] if a["agent"] == "板块 Agent")
        self.assertIn("未匹配", sector_agent["conclusion"])  # 个股未标注手工板块
        self.assertEqual(len(cli._sectors_frame(args)), 1)

    def test_detail_prints_evidence_table(self):
        code, out = self.run_cli([
            "analyze", "--code", "SH.600519", "--offline", "--no-save", "--detail",
        ])
        self.assertEqual(code, 0)
        self.assertIn("证据检查表：", out)
        self.assertIn("决策依据：", out)
        self.assertIn("[低]", out)  # 风控检查项

    def test_invalidation_list_is_flattened(self):
        code, out = self.run_cli([
            "analyze", "--code", "SH.600519", "--offline", "--no-save",
        ])
        self.assertEqual(code, 0)
        self.assertIn("失效条件：", out)
        self.assertNotIn("失效条件：['", out)

    def test_offline_disables_llm_without_persisting_change(self):
        previous = settings.llm_enabled
        settings.llm_enabled = True
        try:
            code, out, data = self.analyze_json([
                "analyze", "--code", "SH.600519", "--offline", "--no-save",
            ])
            self.assertEqual(code, 0, out)
            self.assertTrue(settings.llm_enabled)
            self.assertIn("未启用", data["llm_summary"])
        finally:
            settings.llm_enabled = previous

    def test_no_llm_flag_restores_setting(self):
        previous = settings.llm_enabled
        settings.llm_enabled = True
        try:
            code, _, data = self.analyze_json([
                "analyze", "--code", "SH.600519", "--offline", "--no-llm", "--no-save",
            ])
            self.assertEqual(code, 0)
            self.assertTrue(settings.llm_enabled)
        finally:
            settings.llm_enabled = previous

    def test_json_out_directory_gets_automatic_filename(self):
        code, out = self.run_cli([
            "analyze", "--code", "SH.600519", "--offline",
            "--json-out", str(self.tmp_path),
        ])
        self.assertEqual(code, 0)
        files = sorted(self.tmp_path.glob("trading_agents_*.json"))
        self.assertEqual(len(files), 1, out)
        self.assertIn("trading_agents", json.loads(files[0].read_text(encoding="utf-8")))

    def test_default_autosave_writes_under_output(self):
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            os.chdir(folder)
            try:
                code, out = self.run_cli([
                    "analyze", "--code", "US.AAPL", "--market", "US", "--offline",
                ])
                self.assertEqual(code, 0, out)
                files = sorted(Path("output/trading_agents").glob("*.json"))
                self.assertEqual(len(files), 1)
                self.assertIn("完整结果:", out)
            finally:
                os.chdir(old_cwd)

    def test_no_save_disables_autosave(self):
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            os.chdir(folder)
            try:
                code, out = self.run_cli([
                    "analyze", "--code", "X", "--offline", "--no-save",
                ])
                self.assertEqual(code, 0)
                self.assertNotIn("完整结果:", out)
                self.assertFalse(Path("output/trading_agents").exists())
            finally:
                os.chdir(old_cwd)

    def test_json_default_handles_dataframe_and_numpy_scalar(self):
        frame = pd.DataFrame([{"a": 1}])
        self.assertEqual(cli._json_default(frame), [{"a": 1}])
        import numpy as np
        self.assertEqual(cli._json_default(np.int64(3)), 3)

    def test_runtime_exception_returns_exit_code_1(self):
        with patch.object(cli, "analyze_stock", side_effect=RuntimeError("模拟数据源崩溃")):
            code, out = self.run_cli(["analyze", "--code", "X", "--offline", "--no-save"])
        self.assertEqual(code, 1)
        self.assertIn("执行失败", out)
        self.assertIn("模拟数据源崩溃", out)


class ScanIntegrationTest(CliTestBase):
    def test_legacy_default_scan(self):
        result = _scan_result()
        record = {}
        module = _fake_workflow_module(result, record)
        with patch.dict(sys.modules, {"ashare_hunter.graph.workflow": module}):
            code, out = self.run_cli([])
        self.assertEqual(code, 0, out)
        self.assertIn("A-Share Hunter 扫描完成", out)
        self.assertIn("市场环境: 自定义偏强", out)
        self.assertIn("市值门槛: 50.0 亿元", out)
        self.assertEqual(record, {"min_market_cap_cny": None})

    def test_scan_min_cap_converts_yi_to_yuan(self):
        record = {}
        module = _fake_workflow_module(_scan_result(), record)
        with patch.dict(sys.modules, {"ashare_hunter.graph.workflow": module}):
            code, _ = self.run_cli(["scan", "--min-cap", "20"])
        self.assertEqual(code, 0)
        self.assertEqual(record["min_market_cap_cny"], 20 * 1e8)

    def test_scan_us_uses_us_orchestrator(self):
        result = _scan_result()
        result["market"] = "US"
        holder = {"result": result}

        class USFake:
            def __init__(self, **kwargs):
                holder["kwargs"] = kwargs

            def scan(self, message=None):
                return result

        module = types.ModuleType("ashare_hunter.graph.us_workflow")
        module.USMarketOrchestrator = USFake
        with patch.dict(sys.modules, {"ashare_hunter.graph.us_workflow": module}):
            code, out = self.run_cli(["scan", "--market", "US", "--min-cap", "10"])
        self.assertEqual(code, 0, out)
        self.assertIn("US-Stock Hunter 扫描完成", out)
        self.assertIn("亿美元", out)
        self.assertEqual(holder["kwargs"]["min_market_cap_usd"], 10 * 1e8)

    def test_scan_json_out_exports_scan_result(self):
        result = _scan_result()
        path = self.tmp_path / "scan.json"
        module = _fake_workflow_module(result, {})
        with patch.dict(sys.modules, {"ashare_hunter.graph.workflow": module}):
            code, out = self.run_cli(["scan", "--json-out", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("扫描结果 JSON:", out)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["market_summary"]["market_regime"],
                         "自定义偏强")

    def test_scan_analyze_passes_real_context(self):
        result = _scan_result()
        record = {}
        module = _fake_workflow_module(result, record)
        captured = {}
        real_analyze = cli.analyze_stock
        path = self.tmp_path / "linked.json"

        def spy(args, **kwargs):
            captured.update(kwargs)
            return real_analyze(args, **kwargs)

        with patch.dict(sys.modules, {"ashare_hunter.graph.workflow": module}), \
             patch.object(cli, "analyze_stock", side_effect=spy), \
             patch.object(cli, "_make_provider", return_value=DebugFixtureProvider(
                 "SH.600519", "调试样本", "A", "bull")):
            code, out = self.run_cli([
                "scan", "--analyze", "600519", "--json-out", str(path),
            ])
        self.assertEqual(code, 0, out)
        self.assertEqual(captured["stock"]["code"], "SH.600519")
        self.assertEqual(captured["market_summary"]["market_regime"], "自定义偏强")
        self.assertEqual(list(captured["sectors"]["sector_name"]), ["自定义板块"])
        self.assertIn("候选表命中 是", out)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["market"]["market_regime"], "自定义偏强")
        self.assertIn("trading_agents", data)

    def test_scan_analyze_falls_back_when_code_not_in_candidates(self):
        result = _scan_result()
        module = _fake_workflow_module(result, {})
        captured = {}
        real_analyze = cli.analyze_stock

        def spy(args, **kwargs):
            captured.update(kwargs)
            return {"trading_agents": {}, "agents": [], "stock": kwargs.get("stock") or {}}

        with patch.dict(sys.modules, {"ashare_hunter.graph.workflow": module}), \
             patch.object(cli, "analyze_stock", side_effect=spy):
            code, out = self.run_cli([
                "scan", "--analyze", "SZ.300750", "--no-save",
            ])
        self.assertEqual(code, 0, out)
        self.assertEqual(captured["stock"]["code"], "SZ.300750")
        self.assertIn("候选表命中 否", captured.get("mode", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
