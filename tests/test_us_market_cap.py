"""美股市值门槛离线测试。

覆盖：富途快照市值字段规整、按总市值下限过滤与缺失策略、快照缓存一致性、
编排层兜底过滤与提示、GUI 阈值控件 / 表格格式化 / 深度研究二次防护。
不依赖 Futu OpenD；Qt 以 offscreen 模式运行。
"""

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
from PySide6.QtWidgets import QApplication

from config import settings
from ashare_hunter.data.futu_provider import FutuProvider, filter_by_market_cap
from ashare_hunter.graph.us_workflow import USMarketOrchestrator
from gui import HunterWindow, MarketScanWorker, MARKET_A, MARKET_US

APP = QApplication.instance() or QApplication([])

THRESHOLD = 1_000_000_000.0  # 10 亿美元


def _spot_with_caps():
    """5 只标的：2 只达标（含正好等于门槛）、1 只低市值、2 只市值缺失。"""
    return pd.DataFrame({
        "code": ["BIG1", "BIG2", "SMALL", "NOCAP", "ZERO"],
        "name": ["大盘一", "大盘二", "小盘", "无市值", "零市值"],
        "price": [100.0] * 5,
        "prev_close_price": [99.0] * 5,
        "change_pct": [1.0, 2.0, 3.0, 0.5, -0.5],
        "amount": [5e9] * 5,
        "turnover": [0.5] * 5,
        "volume_ratio": [1.2] * 5,
        "market_cap": [3e12, THRESHOLD, 5e8, float("nan"), 0.0],
        "float_market_cap": [2.9e12, THRESHOLD * 0.9, 4e8, float("nan"), 0.0],
        "market": "US",
    })


class MarketCapFieldTest(unittest.TestCase):
    """_shape_quote 把 Futu 的 total_market_val / circular_market_val 规整为统一字段。"""

    def test_shape_quote_maps_futu_market_value_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = FutuProvider(cache_db=os.path.join(tmp, "cache.sqlite3"),
                             request_interval=0.0)
            basic = pd.DataFrame({
                "code": ["US.AAPL", "US.SMALL", "US.ETF"],
                "name": ["苹果", "小盘", "ETF"],
                "code6": ["AAPL", "SMALL", "ETF"],
            })
            quote = pd.DataFrame({
                "code": ["US.AAPL", "US.SMALL", "US.ETF"],
                "last_price": [100.0] * 3,
                "prev_close_price": [98.0] * 3,
                "turnover": [5e9] * 3,
                "total_market_val": [3e12, 5e8, 0.0],
                "circular_market_val": [2.9e12, 4e8, None],
            })
            df = p._shape_quote(basic, quote)
            by_code = df.set_index("code")
            self.assertAlmostEqual(by_code.loc["AAPL", "market_cap"], 3e12)
            self.assertAlmostEqual(by_code.loc["AAPL", "float_market_cap"], 2.9e12)
            self.assertAlmostEqual(by_code.loc["SMALL", "market_cap"], 5e8)
            # equity_valid=False 时富途返回 0 / N/A：统一视为缺失，不冒充小市值
            self.assertTrue(pd.isna(by_code.loc["ETF", "market_cap"]))
            self.assertTrue(pd.isna(by_code.loc["ETF", "float_market_cap"]))
            p.close()

    def test_missing_market_value_column_still_exposes_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = FutuProvider(cache_db=os.path.join(tmp, "cache.sqlite3"),
                             request_interval=0.0)
            basic = pd.DataFrame({"code": ["US.AAPL"], "name": ["苹果"], "code6": ["AAPL"]})
            quote = pd.DataFrame({
                "code": ["US.AAPL"], "last_price": [100.0],
                "prev_close_price": [98.0], "turnover": [5e9],
            })
            df = p._shape_quote(basic, quote)
            self.assertIn("market_cap", df.columns)
            self.assertIn("float_market_cap", df.columns)
            self.assertTrue(pd.isna(df.iloc[0]["market_cap"]))
            p.close()


class FilterFunctionTest(unittest.TestCase):
    """纯函数级：阈值边界、缺失策略、无字段降级。"""

    def test_below_threshold_removed_and_equal_kept(self):
        df, stats = filter_by_market_cap(_spot_with_caps(), THRESHOLD, "keep")
        self.assertEqual(list(df["code"]), ["BIG1", "BIG2", "NOCAP", "ZERO"])
        self.assertEqual(stats["before"], 5)
        self.assertEqual(stats["after"], 4)
        self.assertEqual(stats["below"], 1)       # 只剔除 SMALL
        self.assertEqual(stats["missing"], 2)     # NaN + 0
        self.assertEqual(stats["missing_excluded"], 0)
        self.assertTrue(stats["enabled"] and stats["has_field"])

    def test_exclude_policy_drops_missing_market_cap(self):
        df, stats = filter_by_market_cap(_spot_with_caps(), THRESHOLD, "exclude")
        self.assertEqual(list(df["code"]), ["BIG1", "BIG2"])
        self.assertEqual(stats["below"], 1)
        self.assertEqual(stats["missing_excluded"], 2)
        self.assertEqual(stats["after"], 2)

    def test_zero_threshold_disables_filter(self):
        df, stats = filter_by_market_cap(_spot_with_caps(), 0.0, "exclude")
        self.assertEqual(len(df), 5)
        self.assertFalse(stats["enabled"])
        self.assertEqual(stats["after"], 5)

    def test_no_market_cap_field_keep(self):
        df = _spot_with_caps().drop(columns=["market_cap", "float_market_cap"])
        out, stats = filter_by_market_cap(df, THRESHOLD, "keep")
        self.assertEqual(len(out), 5)
        self.assertFalse(stats["has_field"])
        self.assertEqual(stats["missing"], 5)

    def test_no_market_cap_field_exclude(self):
        df = _spot_with_caps().drop(columns=["market_cap", "float_market_cap"])
        out, stats = filter_by_market_cap(df, THRESHOLD, "exclude")
        self.assertTrue(out.empty)
        self.assertEqual(stats["missing_excluded"], 5)
        self.assertEqual(stats["after"], 0)

    def test_empty_frame_is_safe(self):
        out, stats = filter_by_market_cap(pd.DataFrame(), THRESHOLD, "keep")
        self.assertTrue(out.empty)
        self.assertEqual(stats["before"], 0)


class ProviderIntegrationTest(unittest.TestCase):
    """真实 FutuProvider 路径：快照过滤 → attrs 统计 → 缓存一致性。"""

    def _provider(self, tmp, threshold=THRESHOLD, policy="keep"):
        p = FutuProvider(
            cache_db=os.path.join(tmp, "cache.sqlite3"),
            request_interval=0.0, snapshot_batch_size=3,
            us_min_market_cap=threshold, us_missing_market_cap=policy,
        )
        codes = ["US.BIG1", "US.BIG2", "US.SMALL", "US.NOCAP", "US.ZERO"]
        basic = pd.DataFrame({
            "code": codes, "name": ["大盘一", "大盘二", "小盘", "无市值", "零市值"],
            "exchange_type": ["US_NASDAQ"] * 5,
        })
        raw = pd.DataFrame({
            "code": codes,
            "last_price": [100.0] * 5,
            "prev_close_price": [99.0] * 5,
            "turnover": [5e9] * 5,
            "turnover_rate": [0.5] * 5,
            "volume": [1e7] * 5,
            "total_market_val": [3e12, THRESHOLD, 5e8, None, 0.0],
            "circular_market_val": [2.9e12, THRESHOLD * 0.9, 4e8, None, 0.0],
        })

        def fake_call(method, *a, **k):
            if method == "get_stock_basicinfo":
                return basic
            if method == "get_market_snapshot":
                chunk = list(a[0])
                return raw[raw["code"].isin(chunk)].reset_index(drop=True)
            raise AssertionError(f"未预期的接口调用：{method}")

        p._call = fake_call
        return p

    def test_get_us_spot_filters_and_reports_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._provider(tmp)
            df = p.get_us_spot()
            self.assertEqual(list(df["code"]), ["BIG1", "BIG2", "NOCAP", "ZERO"])
            stats = df.attrs["market_cap_filter"]
            self.assertEqual(stats["before"], 5)
            self.assertEqual(stats["after"], 4)
            self.assertEqual(stats["below"], 1)
            self.assertEqual(stats["missing"], 2)
            self.assertEqual(df.attrs["otc_excluded"], 0)
            # 缓存必须与返回值一致，否则板块内选股会把小市值股带回来
            self.assertEqual(list(p._spot_cache["code"]), list(df["code"]))
            self.assertEqual(p._spot_market, "US")
            p.close()

    def test_missing_exclude_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._provider(tmp, policy="exclude")
            df = p.get_us_spot()
            self.assertEqual(list(df["code"]), ["BIG1", "BIG2"])
            self.assertEqual(df.attrs["market_cap_filter"]["missing_excluded"], 2)
            p.close()

    def test_zero_threshold_disables_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._provider(tmp, threshold=0.0)
            df = p.get_us_spot()
            self.assertEqual(len(df), 5)
            self.assertFalse(df.attrs["market_cap_filter"]["enabled"])
            p.close()

    def test_runtime_threshold_overrides_constructor(self):
        with tempfile.TemporaryDirectory() as tmp:
            # exclude 策略：市值缺失标的也一并剔除，便于清晰验证新门槛生效
            p = self._provider(tmp, policy="exclude")
            df = p.get_us_spot(min_market_cap=2e12)
            self.assertEqual(list(df["code"]), ["BIG1"])  # BIG2 原本达标，新门槛下被剔除
            self.assertEqual(p.us_min_market_cap, 2e12)
            self.assertEqual(df.attrs["market_cap_filter"]["threshold_usd"], 2e12)
            p.close()

    def test_sector_stocks_reuse_filtered_cache(self):
        """板块成分股缓存里有小市值股时，也因快照缓存已过滤而不会返回。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = self._provider(tmp)
            p.get_us_spot()
            now = "2026-11-18T00:00:00"
            with p._db() as conn:
                conn.executemany(
                    """INSERT OR REPLACE INTO plate_members
                       (plate_code, plate_name, board_type, stock_code, stock_name, market, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [("US.BK0001", "科技", "industry", f"US.{code}", code, "US", now)
                     for code in ("BIG1", "BIG2", "SMALL")],
                )
            rows = p.get_sector_stocks("科技", "industry", market="US")
            self.assertEqual(set(rows["code"]), {"BIG1", "BIG2"})
            self.assertIn("market_cap", rows.columns)
            p.close()

    def test_a_share_snapshot_not_affected_by_us_threshold(self):
        """市值门槛只作用于美股；A 股 get_spot 仍返回全部标的。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = FutuProvider(
                cache_db=os.path.join(tmp, "cache.sqlite3"),
                request_interval=0.0, us_min_market_cap=THRESHOLD,
            )
            basic = pd.DataFrame({
                "code": ["SH.600519", "SZ.000001"],
                "name": ["贵州茅台", "平安银行"],
            })
            raw = pd.DataFrame({
                "code": ["SH.600519", "SZ.000001"],
                "last_price": [100.0, 10.0],
                "prev_close_price": [98.0, 9.9],
                "turnover": [5e9, 1e9],
                "total_market_val": [3e12, 5e8],
            })

            def fake_call(method, *a, **k):
                if method == "get_stock_basicinfo":
                    return basic
                chunk = list(a[0])
                return raw[raw["code"].isin(chunk)].reset_index(drop=True)

            p._call = fake_call
            df = p.get_spot()
            self.assertEqual(len(df), 2)
            self.assertIn("market_cap", df.columns)
            self.assertEqual(p._spot_market, "SH")
            p.close()


class _BareProvider:
    """不带数据层过滤能力的离线 Provider，用于验证编排层兜底。"""

    def __init__(self, spot):
        self.spot = spot
        self._spot_cache = None
        self._spot_market = "US"

    def get_us_spot(self, symbols=None):
        return self.spot.copy()

    def get_sectors(self, spot, market="SH"):
        raise RuntimeError("测试：跳过板块阶段")


class OrchestratorMarketCapTest(unittest.TestCase):
    def test_scan_falls_back_to_filter_and_emits_notice(self):
        provider = _BareProvider(_spot_with_caps())
        messages = []
        result = USMarketOrchestrator(
            provider, min_market_cap_usd=THRESHOLD
        ).scan(message=messages.append)
        self.assertEqual(result["market_cap_filter"]["after"], 4)
        self.assertEqual(len(result["spot"]), 4)
        self.assertTrue(any("市值门槛" in m for m in messages))
        self.assertTrue(any("市值缺失" in m for m in messages))
        self.assertEqual(len(provider._spot_cache), 4)  # 缓存同步，避免回带小盘股

    def test_scan_with_disabled_threshold(self):
        messages = []
        result = USMarketOrchestrator(
            _BareProvider(_spot_with_caps()), min_market_cap_usd=0
        ).scan(message=messages.append)
        self.assertEqual(len(result["spot"]), 5)
        self.assertFalse(result["market_cap_filter"]["enabled"])
        self.assertFalse(any("市值门槛" in m for m in messages))

    def test_scan_exclude_policy_carries_through(self):
        result = USMarketOrchestrator(
            _BareProvider(_spot_with_caps()),
            min_market_cap_usd=THRESHOLD, missing_market_cap="exclude",
        ).scan()
        self.assertEqual(result["market_cap_filter"]["after"], 2)
        self.assertEqual(len(result["spot"]), 2)

    def test_scan_without_market_cap_field_warns(self):
        spot = _spot_with_caps().drop(columns=["market_cap", "float_market_cap"])
        messages = []
        result = USMarketOrchestrator(
            _BareProvider(spot), min_market_cap_usd=THRESHOLD
        ).scan(message=messages.append)
        self.assertFalse(result["market_cap_filter"]["has_field"])
        self.assertEqual(len(result["spot"]), 5)  # keep 策略：不编造、不误杀
        self.assertTrue(any("未提供总市值字段" in m for m in messages))


class MarketCapGuiTest(unittest.TestCase):
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
    def _result(cap_filter=None, stocks=None):
        return {
            "market": "US",
            "currency": "USD",
            "scan_time": "2026-11-18T23:00:00+08:00",
            "market_cap_filter": cap_filter or {},
            "market_summary": {
                "market_regime": "偏强", "advance_decline_ratio": 0.6,
                "up_count": 3, "down_count": 2, "flat_count": 0,
                "avg_change_pct": 1.2, "total_amount_yuan": 8e9,
                "amount_note": "成交额为美元",
            },
            "top_sectors": pd.DataFrame(),
            "top_stocks": stocks if stocks is not None else pd.DataFrame([{
                "code": "BIG1", "name": "大盘一", "change_pct": 2.1,
                "amount": 5e9, "score": 80.0, "market_cap": 3e12,
            }]),
            "explanation": "市值过滤后的研究解读",
        }

    def test_spin_default_comes_from_settings(self):
        # A 股模式默认值为 50 亿元，切到美股恢复 10 亿美元
        self.assertAlmostEqual(
            self.window.min_cap_spin.value(), settings.a_min_market_cap_cny / 1e8
        )
        self.assertIn("亿元", self.window.min_cap_spin.suffix())
        self.window.market_combo.setCurrentIndex(1)
        self.assertAlmostEqual(
            self.window.min_cap_spin.value(), settings.us_min_market_cap_usd / 1e8
        )
        self.assertIn("亿美元", self.window.min_cap_spin.suffix())

    def test_cap_widget_visible_in_both_markets(self):
        self.assertFalse(self.window.cap_label.isHidden())
        self.assertFalse(self.window.min_cap_spin.isHidden())
        self.window.market_combo.setCurrentIndex(1)
        self.assertFalse(self.window.cap_label.isHidden())
        self.assertFalse(self.window.min_cap_spin.isHidden())

    def test_threshold_converted_per_market(self):
        self.window.market_combo.setCurrentIndex(1)
        self.window.min_cap_spin.setValue(25.0)
        self.assertAlmostEqual(self.window.min_market_cap(), 2.5e9)
        self.window.market_combo.setCurrentIndex(0)
        self.window.min_cap_spin.setValue(3.0)
        self.assertAlmostEqual(self.window.min_market_cap(), 3e8)
        # 两个市场各自记忆，切回美股仍是 25 亿美元
        self.window.market_combo.setCurrentIndex(1)
        self.assertAlmostEqual(self.window.min_cap_spin.value(), 25.0)

    def test_scan_worker_carries_threshold(self):
        worker = MarketScanWorker(market=MARKET_US, min_market_cap=2e9)
        self.assertEqual(worker.min_market_cap, 2e9)
        self.assertEqual(worker.market, MARKET_US)
        a_worker = MarketScanWorker(market=MARKET_A, min_market_cap=5e9)
        self.assertEqual(a_worker.min_market_cap, 5e9)

    def test_market_cap_column_formatted_in_hundred_million_usd(self):
        self.window.market_combo.setCurrentIndex(1)
        cap_filter = {
            "enabled": True, "threshold_usd": THRESHOLD, "has_field": True,
            "before": 5, "after": 4, "below": 1, "missing": 2,
            "missing_excluded": 0, "missing_policy": "keep",
        }
        self.window.on_completed(self._result(cap_filter=cap_filter))
        headers = [
            self.window.stock_table.horizontalHeaderItem(i).text()
            for i in range(self.window.stock_table.columnCount())
        ]
        col = headers.index("market_cap")
        self.assertIn("亿美元", self.window.stock_table.item(0, col).text())
        self.assertIn("市值门槛", self.window.market_text.toPlainText())

    def test_analysis_guard_blocks_below_threshold_stock(self):
        self.window.market_combo.setCurrentIndex(1)
        cap_filter = {
            "enabled": True, "threshold_usd": 1e12, "has_field": True,
            "before": 2, "after": 1, "below": 1, "missing": 0,
            "missing_excluded": 0, "missing_policy": "keep",
        }
        stocks = pd.DataFrame([{
            "code": "SMALL", "name": "小盘", "change_pct": 3.0,
            "amount": 5e9, "score": 90.0, "market_cap": 5e8,
        }])
        self.window.on_completed(self._result(cap_filter=cap_filter, stocks=stocks))
        self.window.stock_table.setCurrentCell(0, 0)
        with patch("gui.QMessageBox.information") as info:
            self.window.analyze_selected_stock()
        info.assert_called_once()
        self.assertIn("低于市值门槛", info.call_args[0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
