"""A 股市值门槛离线测试。

覆盖：A 股独立配置、按人民币元口径过滤与缺失策略、快照缓存一致性、
板块成分股回流防护、LangGraph 工作流回传与兜底、GUI 双市场单位隔离与
深度研究二次防护、两个市场门槛互不干扰。不依赖 Futu OpenD；Qt offscreen。
"""

import os
import tempfile
import unittest
from unittest.mock import patch as mock_patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
from PySide6.QtWidgets import QApplication

from config import settings
from ashare_hunter.data.futu_provider import FutuProvider, filter_by_market_cap
from ashare_hunter.graph.workflow import build_workflow
from gui import HunterWindow, MarketScanWorker, MARKET_A, MARKET_US

APP = QApplication.instance() or QApplication([])

THRESHOLD = 5_000_000_000.0  # 50 亿元
CODES = ["SH.600519", "SZ.000001", "SZ.300001", "SH.688001"]
NAMES = ["贵州茅台", "平安银行", "小盘股", "无市值"]
CAPS = [2e12, THRESHOLD, 8e8, None]


def _a_spot():
    return pd.DataFrame({
        "code": ["600519", "000001", "300001", "688001"],
        "name": NAMES,
        "price": [100.0] * 4,
        "prev_close_price": [99.0] * 4,
        "change_pct": [1.0, 2.0, 3.0, 0.5],
        "amount": [5e9] * 4,
        "turnover": [0.5] * 4,
        "volume_ratio": [1.2] * 4,
        "market_cap": list(map(float, [
            2e12, THRESHOLD, 8e8, float("nan")
        ])),
        "float_market_cap": [1.9e12, THRESHOLD * 0.9, 7e8, float("nan")],
    })


class AShareConfigTest(unittest.TestCase):
    def test_settings_expose_a_share_threshold(self):
        self.assertGreater(settings.a_min_market_cap_cny, 0)
        self.assertIn(settings.a_missing_market_cap, ("keep", "exclude"))
        # 两个市场门槛独立配置，默认都不为 0
        self.assertGreater(settings.us_min_market_cap_usd, 0)

    def test_filter_marks_cny_market_and_threshold_alias(self):
        out, stats = filter_by_market_cap(_a_spot(), THRESHOLD, "keep", market="SH")
        self.assertEqual(list(out["code"]), ["600519", "000001", "688001"])
        self.assertEqual(stats["market"], "SH")
        self.assertEqual(stats["currency"], "CNY")
        self.assertEqual(stats["threshold"], THRESHOLD)
        self.assertEqual(stats["threshold_usd"], THRESHOLD)  # 兼容别名
        self.assertEqual(stats["below"], 1)
        self.assertEqual(stats["missing"], 1)

    def test_filter_us_still_marks_usd(self):
        _, stats = filter_by_market_cap(_a_spot(), THRESHOLD, "keep", market="US")
        self.assertEqual(stats["market"], "US")
        self.assertEqual(stats["currency"], "USD")

    def test_no_market_cap_field_keep_does_not_wipe(self):
        df = _a_spot().drop(columns=["market_cap", "float_market_cap"])
        out, stats = filter_by_market_cap(df, THRESHOLD, "keep", market="SH")
        self.assertEqual(len(out), 4)
        self.assertFalse(stats["has_field"])
        self.assertEqual(stats["missing"], 4)


class AShareProviderTest(unittest.TestCase):
    def _provider(self, tmp, threshold=THRESHOLD, policy="keep", **extra):
        p = FutuProvider(
            cache_db=os.path.join(tmp, "cache.sqlite3"),
            request_interval=0.0, snapshot_batch_size=3,
            a_min_market_cap=threshold, a_missing_market_cap=policy, **extra,
        )
        basic = pd.DataFrame({"code": CODES, "name": NAMES})
        raw = pd.DataFrame({
            "code": CODES,
            "last_price": [100.0] * 4,
            "prev_close_price": [99.0] * 4,
            "turnover": [5e9] * 4,
            "turnover_rate": [0.5] * 4,
            "volume": [1e7] * 4,
            "total_market_val": CAPS,
            "circular_market_val": CAPS,
        })

        def fake_call(method, *a, **k):
            if method == "get_stock_basicinfo":
                return basic
            if method == "get_market_snapshot":
                return raw[raw["code"].isin(list(a[0]))].reset_index(drop=True)
            raise AssertionError(f"未预期的接口调用：{method}")

        p._call = fake_call
        return p

    def test_get_spot_filters_and_reports_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._provider(tmp)
            df = p.get_spot()
            self.assertEqual(list(df["code"]), ["600519", "000001", "688001"])
            stats = df.attrs["market_cap_filter"]
            self.assertEqual(stats["before"], 4)
            self.assertEqual(stats["after"], 3)
            self.assertEqual(stats["below"], 1)
            self.assertEqual(stats["missing"], 1)
            self.assertEqual(stats["currency"], "CNY")
            self.assertEqual(list(p._spot_cache["code"]), list(df["code"]))
            self.assertEqual(p._spot_market, "SH")
            p.close()

    def test_exclude_policy_drops_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._provider(tmp, policy="exclude")
            df = p.get_spot()
            self.assertEqual(list(df["code"]), ["600519", "000001"])
            self.assertEqual(df.attrs["market_cap_filter"]["missing_excluded"], 1)
            p.close()

    def test_zero_threshold_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._provider(tmp, threshold=0.0)
            df = p.get_spot()
            self.assertEqual(len(df), 4)
            self.assertFalse(df.attrs["market_cap_filter"]["enabled"])
            p.close()

    def test_runtime_threshold_overrides_constructor(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._provider(tmp, policy="exclude")
            df = p.get_spot(min_market_cap=1e12)
            self.assertEqual(list(df["code"]), ["600519"])
            self.assertEqual(p.a_min_market_cap, 1e12)
            self.assertEqual(df.attrs["market_cap_filter"]["threshold"], 1e12)
            p.close()

    def test_sector_stocks_reuse_filtered_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._provider(tmp)
            p.get_spot()
            now = "2026-11-18T00:00:00"
            with p._db() as conn:
                conn.executemany(
                    """INSERT OR REPLACE INTO plate_members
                       (plate_code, plate_name, board_type, stock_code, stock_name, market, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [("SH.BK0001", "白酒", "industry", code, name, "SH", now)
                     for code, name in zip(CODES, NAMES)],
                )
            rows = p.get_sector_stocks("白酒", "industry", market="SH")
            self.assertEqual(set(rows["code"]), {"600519", "000001", "688001"})
            self.assertIn("market_cap", rows.columns)
            p.close()

    def test_us_threshold_does_not_affect_a_share(self):
        """美股设极高门槛，A 股快照仍按自己的门槛过滤。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = self._provider(tmp, us_min_market_cap=1e13)
            df = p.get_spot()
            self.assertEqual(len(df), 3)
            self.assertEqual(df.attrs["market_cap_filter"]["currency"], "CNY")
            p.close()

    def test_a_threshold_does_not_affect_us_spot(self):
        """A 股设极高门槛，美股快照仍按自己的（关闭）门槛返回全部。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = self._provider(tmp, threshold=1e13, us_min_market_cap=0.0)
            raw = pd.DataFrame({
                "code": ["US.AAPL"], "name": ["苹果"], "last_price": [100.0],
                "prev_close_price": [98.0], "turnover": [5e9],
                "total_market_val": [3e12],
            })
            original = p._call
            p._call = lambda method, *a, **k: (
                raw[raw["code"].isin(list(a[0]))].reset_index(drop=True)
                if method == "get_market_snapshot" else original(method, *a, **k)
            )
            df = p.get_us_spot(symbols=["US.AAPL"])
            self.assertEqual(list(df["code"]), ["AAPL"])
            self.assertFalse(df.attrs["market_cap_filter"]["enabled"])
            p.close()


class _WorkflowProvider:
    """离线工作流替身：不带数据层过滤能力，用于验证编排层兜底。"""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._spot_cache = None
        self._spot_market = "SH"
        self.min_market_cap_used = None
        self.sectors_spot = None
        self.closed = False
        _WorkflowProvider.instances.append(self)

    def get_spot(self, min_market_cap=None):
        self.min_market_cap_used = min_market_cap
        return _a_spot().copy()  # 无 attrs，触发编排层兜底过滤

    def get_sectors(self, spot, market="SH"):
        self.sectors_spot = spot
        return pd.DataFrame([{"sector_name": "白酒", "change_pct": 2.0}])

    def close(self):
        self.closed = True


class _CaptureGraph:
    """LangGraph 替身：只捕获节点函数，避免跑完整扫描链路。"""

    def __init__(self, state_type):
        self.nodes = {}

    def add_node(self, name, fn):
        self.nodes[name] = fn

    def set_entry_point(self, name):
        pass

    def add_edge(self, a, b):
        pass

    def compile(self):
        return self


class AShareWorkflowTest(unittest.TestCase):
    def _collect(self, threshold=THRESHOLD, policy=None):
        _WorkflowProvider.instances.clear()
        with mock_patch("ashare_hunter.graph.workflow.FutuProvider", _WorkflowProvider), \
             mock_patch("ashare_hunter.graph.workflow.StateGraph", _CaptureGraph):
            graph = build_workflow(
                min_market_cap_cny=threshold, missing_market_cap=policy
            )
            out = graph.nodes["collect"]({})
        provider = _WorkflowProvider.instances[-1]
        return out, provider

    def test_workflow_passes_a_share_threshold_to_provider(self):
        out, provider = self._collect()
        self.assertEqual(provider.kwargs["a_min_market_cap"], THRESHOLD)
        self.assertEqual(provider.kwargs["a_missing_market_cap"],
                         settings.a_missing_market_cap)
        self.assertEqual(provider.min_market_cap_used, THRESHOLD)

    def test_collect_falls_back_to_filter_and_syncs_cache(self):
        out, provider = self._collect()
        self.assertEqual(len(out["spot"]), 3)
        self.assertEqual(out["market_cap_filter"]["after"], 3)
        self.assertEqual(out["market_cap_filter"]["currency"], "CNY")
        # 板块聚合与快照缓存必须用同一份过滤后的快照
        self.assertEqual(len(provider.sectors_spot), 3)
        self.assertEqual(len(provider._spot_cache), 3)
        self.assertEqual(provider._spot_market, "SH")

    def test_disabled_threshold_keeps_all(self):
        out, _ = self._collect(threshold=0.0)
        self.assertEqual(len(out["spot"]), 4)
        self.assertFalse(out["market_cap_filter"]["enabled"])

    def test_exclude_policy_flows_through_workflow(self):
        out, provider = self._collect(policy="exclude")
        self.assertEqual(len(out["spot"]), 2)
        self.assertEqual(provider.kwargs["a_missing_market_cap"], "exclude")


class AShareGuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.window = HunterWindow()

    def setUp(self):
        self.window._cap_values = {
            MARKET_A: settings.a_min_market_cap_cny / 1e8,
            MARKET_US: settings.us_min_market_cap_usd / 1e8,
        }
        self.window.market_combo.setCurrentIndex(0)
        self.window.min_cap_spin.setValue(self.window._cap_values[MARKET_A])

    @staticmethod
    def _result(cap_filter=None, stocks=None, threshold=THRESHOLD):
        return {
            "market": "A",
            "scan_time": "2026-11-18T23:00:00+08:00",
            "market_cap_filter": cap_filter if cap_filter is not None else {
                "enabled": True, "threshold": threshold, "has_field": True,
                "before": 4, "after": 3, "below": 1, "missing": 1,
                "missing_excluded": 0, "missing_policy": "keep", "currency": "CNY",
            },
            "market_summary": {
                "market_regime": "偏强", "advance_decline_ratio": 0.6,
                "up_count": 3, "down_count": 1, "flat_count": 0,
                "avg_change_pct": 1.2, "total_amount_yuan": 8e9,
            },
            "top_sectors": pd.DataFrame(),
            "top_stocks": stocks if stocks is not None else pd.DataFrame([{
                "code": "600519", "name": "贵州茅台", "change_pct": 2.1,
                "amount": 5e9, "score": 80.0, "market_cap": 2e12,
            }]),
            "explanation": "A 股市值过滤后的研究解读",
            "report_path": "output/report.md",
        }

    def test_a_share_default_and_unit(self):
        self.assertAlmostEqual(self.window.min_cap_spin.value(),
                               settings.a_min_market_cap_cny / 1e8)
        self.assertIn("亿元", self.window.min_cap_spin.suffix())
        self.assertNotIn("亿美元", self.window.min_cap_spin.suffix())
        self.assertAlmostEqual(self.window.min_market_cap(),
                               settings.a_min_market_cap_cny)

    def test_cap_values_are_independent_per_market(self):
        self.window.market_combo.setCurrentIndex(1)
        self.window.min_cap_spin.setValue(25.0)
        self.window.market_combo.setCurrentIndex(0)
        self.assertAlmostEqual(self.window.min_cap_spin.value(), 50.0)
        self.window.market_combo.setCurrentIndex(1)
        self.assertAlmostEqual(self.window.min_cap_spin.value(), 25.0)
        self.assertIn("亿美元", self.window.min_cap_spin.suffix())

    def test_worker_carries_a_share_threshold(self):
        worker = MarketScanWorker(
            market=MARKET_A, min_market_cap=THRESHOLD, missing_market_cap="exclude"
        )
        self.assertEqual(worker.min_market_cap, THRESHOLD)
        self.assertEqual(worker.missing_market_cap, "exclude")

    def test_overview_shows_threshold_in_yi_yuan(self):
        self.window.on_completed(self._result())
        text = self.window.market_text.toPlainText()
        self.assertIn("市值门槛", text)
        self.assertIn("亿元", text)
        self.assertNotIn("亿美元", text)

    def test_table_formats_market_cap_in_yi_yuan(self):
        self.window.on_completed(self._result())
        headers = [self.window.stock_table.horizontalHeaderItem(i).text()
                   for i in range(self.window.stock_table.columnCount())]
        col = headers.index("market_cap")
        self.assertIn("亿元", self.window.stock_table.item(0, col).text())

    def test_analysis_guard_blocks_below_threshold_a_share(self):
        stocks = pd.DataFrame([{
            "code": "300001", "name": "小盘股", "change_pct": 3.0,
            "amount": 5e9, "score": 90.0, "market_cap": 8e8,
        }])
        cap_filter = {
            "enabled": True, "threshold": 1e12, "has_field": True,
            "before": 2, "after": 1, "below": 1, "missing": 0,
            "missing_excluded": 0, "missing_policy": "keep",
        }
        self.window.on_completed(self._result(cap_filter=cap_filter, stocks=stocks))
        self.window.stock_table.setCurrentCell(0, 0)
        with mock_patch("gui.QMessageBox.information") as info:
            self.window.analyze_selected_stock()
        info.assert_called_once()
        self.assertIn("低于市值门槛", info.call_args[0][1])
        self.assertIsNone(self.window.analysis_worker)

    def test_scan_status_label_includes_a_share_threshold(self):
        # 用 Worker 替身避免真实启动扫描线程，只验证状态栏文案与传参
        with mock_patch("gui.MarketScanWorker") as worker_cls:
            self.window.start_scan()
        self.assertIn("亿元", self.window.status_label.text())
        self.assertIn("最低总市值", self.window.log_text.toPlainText())
        kwargs = worker_cls.call_args.kwargs
        self.assertEqual(kwargs["market"], MARKET_A)
        self.assertAlmostEqual(kwargs["min_market_cap"], THRESHOLD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
