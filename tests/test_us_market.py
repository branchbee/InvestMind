"""美股市场研究离线测试：代码规范化、快照 schema、板块扫描、四层复用。

不依赖 Futu OpenD，使用模拟 Provider 验证逻辑正确性。
"""

import unittest

import pandas as pd

from ashare_hunter.data.futu_provider import FutuProvider
from ashare_hunter.graph.us_workflow import USMarketOrchestrator
from ashare_hunter.agents.news_agent import NewsAgent
from ashare_hunter.agents.financial_agent import FinancialAgent
from ashare_hunter.agents.stock_analysis import LiquidityAgent


class USCodeNormalisationTest(unittest.TestCase):
    def test_a_share_codes_unchanged(self):
        self.assertEqual(FutuProvider._normalise_code("600519"), "SH.600519")
        self.assertEqual(FutuProvider._normalise_code("SH.600519"), "SH.600519")
        self.assertEqual(FutuProvider._normalise_code("600519.SH"), "SH.600519")
        self.assertEqual(FutuProvider._normalise_code("000001"), "SZ.000001")
        self.assertEqual(FutuProvider._normalise_code("600519"), "SH.600519")

    def test_us_codes(self):
        self.assertEqual(FutuProvider._normalise_code("AAPL"), "US.AAPL")
        self.assertEqual(FutuProvider._normalise_code("us.nvda"), "US.NVDA")
        self.assertEqual(FutuProvider._normalise_code("AAPL.US"), "US.AAPL")
        self.assertEqual(FutuProvider._normalise_code("BRK.B"), "US.BRK.B")
        self.assertEqual(FutuProvider._normalise_code("US.BRK.B"), "US.BRK.B")
        self.assertEqual(FutuProvider._normalise_code("BRK.B.US"), "US.BRK.B")

    def test_code_body(self):
        self.assertEqual(FutuProvider._code6("600519"), "600519")
        self.assertEqual(FutuProvider._code6("US.AAPL"), "AAPL")
        self.assertEqual(FutuProvider._code6("US.BRK.B"), "BRK.B")
        self.assertEqual(FutuProvider._code6("600519.SH"), "600519")


class USNewsMatchingTest(unittest.TestCase):
    def test_ticker_matching(self):
        for raw, expected in [("US.AAPL", "AAPL"), ("AAPL.US", "AAPL"),
                              ("AAPL", "AAPL"), ("BRK.B", "BRK.B"),
                              ("SH.600519", "600519")]:
            self.assertEqual(NewsAgent._normalise_code(raw), expected)

    def test_related_filter_keeps_own_ticker(self):
        rows = {"available": True, "rows": [
            {"title": "Apple announces new product", "related_securities": ["US.AAPL"]},
            {"title": "Microsoft raises guidance", "related_securities": ["US.MSFT"]},
        ]}
        result = NewsAgent().run(notices=rows, stock_name="Apple", stock_code="AAPL")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["filtered_unrelated_count"], 1)


class USFinancialAliasTest(unittest.TestCase):
    def test_english_filing_fields(self):
        key = {"available": True, "reports": [{"date_time_str": "2025Q4", "item_list": [
            {"display_name": "Return on Equity", "data": "25.5"},
            {"display_name": "Gross Profit Margin", "data": "45.2"},
        ]}]}
        income = {"available": True, "reports": [{"date_time_str": "2025Q4", "item_list": [
            {"display_name": "Total Revenue", "data": "100000", "yoy": "15"},
            {"display_name": "Net Income", "data": "20000", "yoy": "22"},
        ]}]}
        result = FinancialAgent().run(key, income)
        self.assertEqual(result["status"], "完成")
        self.assertAlmostEqual(result["roe_pct"], 25.5)
        self.assertAlmostEqual(result["gross_margin_pct"], 45.2)
        self.assertAlmostEqual(result["revenue_yoy_pct"], 15.0)
        self.assertAlmostEqual(result["net_profit_yoy_pct"], 22.0)
        self.assertIsNotNone(result["score"])


class USCurrencyDisplayTest(unittest.TestCase):
    def test_usd_amount_unit(self):
        result = LiquidityAgent().run({"amount": 5e8, "market": "US"})
        self.assertIn("亿美元", result["conclusion"])
        self.assertNotIn("亿元", result["conclusion"].replace("亿美元", ""))

    def test_cny_default_unchanged(self):
        result = LiquidityAgent().run({"amount": 5e8})
        self.assertIn("亿元", result["conclusion"])


def _us_spot():
    return pd.DataFrame({
        "code": ["AAPL", "MSFT", "NVDA", "TSLA"],
        "name": ["Apple", "Microsoft", "NVIDIA", "Tesla"],
        "price": [100.0, 200.0, 300.0, 150.0],
        "prev_close_price": [98.0, 198.0, 290.0, 155.0],
        "change_pct": [2.04, 1.01, 3.45, -3.23],
        "amount": [5e9, 4e9, 8e9, 6e9],
        "turnover": [0.5, 0.4, 0.9, 0.7],
        "volume_ratio": [1.2, 0.9, 1.8, 1.5],
        "code6": ["AAPL", "MSFT", "NVDA", "TSLA"],
        "market": "US",
    })


class USSnapshotSchemaTest(unittest.TestCase):
    def test_get_us_spot_shapes_real_snapshot(self):
        """模拟 Futu 美股快照，验证 get_us_spot 输出与 A 股一致的字段。"""
        provider = FutuProvider(request_interval=0.0)
        raw = pd.DataFrame({
            "code": ["US.AAPL", "US.MSFT"],
            "name": ["苹果", "微软"],
            "last_price": [100.0, 200.0],
            "prev_close_price": [98.0, 202.0],
            "turnover": [5e9, 4e9],
            "turnover_rate": [0.5, 0.4],
            "volume": [1e7, 8e6],
        })
        provider._call = lambda *a, **k: raw

        df = provider.get_us_spot(["AAPL", "MSFT"])

        self.assertEqual(list(df["code"]), ["AAPL", "MSFT"])
        self.assertIn("price", df.columns)
        self.assertIn("amount", df.columns)
        self.assertIn("change_pct", df.columns)
        self.assertIn("volume_ratio", df.columns)
        self.assertAlmostEqual(df.loc[df["code"] == "AAPL", "change_pct"].iloc[0], 100 / 98 * 100 - 100, places=8)
        self.assertEqual(df.loc[df["code"] == "AAPL", "amount"].iloc[0], 5e9)
        self.assertEqual(df.loc[df["code"] == "AAPL", "market"].iloc[0], "US")

    def test_get_us_spot_market_cache_flag(self):
        """全市场路径设置 _spot_market=US，避免与 A 股快照缓存串用。"""
        provider = FutuProvider(request_interval=0.0)
        basic = pd.DataFrame({"code": ["US.AAPL"], "name": ["Apple"], "code6": ["AAPL"]})
        raw = pd.DataFrame({
            "code": ["US.AAPL"], "last_price": [100.0], "prev_close_price": [98.0],
            "turnover": [5e9], "turnover_rate": [0.5], "volume": [1e7],
        })
        provider._call = lambda *a, **k: basic if "basicinfo" in str(a) else raw
        # 分别 stub 两个调用
        def fake_call(method, *a, **k):
            return basic if method == "get_stock_basicinfo" else raw
        provider._call = fake_call
        df = provider.get_us_spot()
        self.assertEqual(provider._spot_market, "US")
        self.assertEqual(df.iloc[0]["code"], "AAPL")


class FakeUSProvider:
    """模拟美股 Provider：提供观察池快照与个股分析所需接口。"""

    def __init__(self):
        self.requested_symbols = None

    def get_us_spot(self, symbols=None):
        return _us_spot()

    def get_sectors(self, spot, market="SH"):
        self.board_scan_market = market
        return pd.DataFrame([{
            "sector_name": "科技", "board_type": "industry", "change_pct": 2.1,
            "up_count": 3, "down_count": 1, "amount": 1.7e10,
            "leader_change_pct": 3.45, "leader_name": "NVIDIA",
        }])

    def get_sector_stocks(self, name, kind, market="SH"):
        assert market == "US"
        return _us_spot()[["code", "name", "change_pct", "amount", "turnover", "volume_ratio"]].copy()

    def get_daily_history(self, code, days=80):
        dates = pd.date_range("2026-01-01", periods=90, freq="B")
        return pd.DataFrame({
            "date": [d.strftime("%Y-%m-%d") for d in dates],
            "close": [10 + i * 0.05 for i in range(90)],
            "pct_change": [0.5] * 90,
        })

    def get_financials(self, code, statement_type=4, financial_type="ANNUAL", num=5):
        if statement_type == 4:
            return {"available": True, "reports": [{"date_time_str": "2025Q4", "item_list": [
                {"display_name": "Return on Equity", "data": 20.0},
            ]}], "structure_list": []}
        return {"available": True, "reports": [{"date_time_str": "2025Q4", "item_list": [
            {"display_name": "Total Revenue", "data": 1e10, "yoy": 18.0},
            {"display_name": "Net Income", "data": 2e9, "yoy": 25.0},
        ]}], "structure_list": []}

    def get_single_snapshot(self, code):
        return {"available": True, "pe_ttm_ratio": 28.0, "pb_ratio": 6.0, "ps_ratio": 5.0}

    def get_valuation_detail(self, code, valuation_type="PE", interval_type="YEAR_5"):
        return {"available": True, "detail": {"trend": {"valuation_percentile": 0.4}}}

    def get_news(self, keyword, max_count=20, news_sub_type="ALL"):
        return {"available": False, "reason": "测试环境无资讯接口"}

    def get_capital_flow(self, code, period_type="DAY", days=10):
        return {"available": False, "reason": "测试环境无资金接口"}

    def get_capital_distribution(self, code):
        return {"available": False, "reason": "测试环境无资金接口"}

    def get_institutional_holdings(self, code, num=10):
        return {"available": False, "reason": "测试环境无机构接口"}

    def get_analyst_consensus(self, code):
        return {"available": False, "reason": "测试环境无评级接口"}


class USOrchestratorTest(unittest.TestCase):
    def test_scan_output_schema_with_sectors(self):
        provider = FakeUSProvider()
        result = USMarketOrchestrator(provider).scan()
        self.assertEqual(result["market"], "US")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(provider.board_scan_market, "US")  # 板块按美股市场扫描
        self.assertIn("market_regime", result["market_summary"])
        self.assertEqual(result["market_summary"]["currency"], "USD")
        self.assertFalse(result["top_sectors"].empty)
        self.assertIn("score", result["top_sectors"].columns)
        self.assertEqual(result["top_sectors"].iloc[0]["sector_name"], "科技")
        self.assertFalse(result["top_stocks"].empty)
        self.assertIn("market", result["top_stocks"].columns)
        self.assertIn("ma5", result["top_stocks"].columns)
        self.assertTrue(result["explanation"])

    def test_no_watchlist_key_in_result(self):
        """观察池已移除：结果不应再含 watchlist 字段。"""
        result = USMarketOrchestrator(FakeUSProvider()).scan()
        self.assertNotIn("watchlist", result)

    def test_scan_empty_market(self):
        class EmptyProvider(FakeUSProvider):
            def get_us_spot(self, symbols=None):
                return pd.DataFrame()

        result = USMarketOrchestrator(EmptyProvider()).scan()
        self.assertTrue(result["top_stocks"].empty)
        self.assertEqual(result["market_summary"]["market_regime"], "数据不足")

    def test_scan_sector_failure_degrades(self):
        """板块接口失败时降级为空板块，不中断整体扫描。"""
        class NoSectorProvider(FakeUSProvider):
            def get_sectors(self, spot, market="SH"):
                raise RuntimeError("无板块权限")

        messages = []
        result = USMarketOrchestrator(NoSectorProvider()).scan(message=messages.append)
        self.assertTrue(result["top_sectors"].empty)
        self.assertTrue(result["top_stocks"].empty)
        self.assertTrue(any("板块聚合暂不可用" in m for m in messages))

    def test_deep_research_reuses_four_layers(self):
        provider = FakeUSProvider()
        result = USMarketOrchestrator(provider).analyze(
            {"code": "AAPL", "name": "Apple", "amount": 5e9},
            market={"market_regime": "偏强"}, sectors=None,
        )
        ta = result.get("trading_agents")
        self.assertIsNotNone(ta)
        self.assertEqual(result["stock"]["market"], "US")
        self.assertEqual(result["stock"]["currency"], "USD")
        for key in ("sentiment", "research", "trading", "risk", "final_recommendation"):
            self.assertIn(key, ta)
        self.assertIn(ta["trading"]["action"], ["买入", "持有", "卖出"])


class USOTCSnapshotTest(unittest.TestCase):
    """回归：美股 OTC / 无行情权限标的导致 get_market_snapshot 整批失败。

    富途对快照是「整批语义」，批内任一代码无行情权限即整批报错
    （如「暂不提供美股 OTC 市场行情 2578256D」）。验证源头过滤 +
    批次二分容错两层防御，以及编排层的提示链路。
    """

    OTC = "US.2578256D"
    NO_RIGHT = "US.BADQ"

    @staticmethod
    def _basicinfo(with_exchange_type=True):
        rows = [
            {"code": "US.AAPL", "name": "Apple", "exchange_type": "US_NASDAQ"},
            {"code": "US.MSFT", "name": "Microsoft", "exchange_type": "US_NASDAQ"},
            {"code": "US.NVDA", "name": "NVIDIA", "exchange_type": "US_NASDAQ"},
            {"code": "US.2578256D", "name": "OTC Sample", "exchange_type": "US_PINK"},
            {"code": "US.BADQ", "name": "No Right", "exchange_type": "US_NYSE"},
        ]
        df = pd.DataFrame(rows)
        return df if with_exchange_type else df.drop(columns=["exchange_type"])

    @staticmethod
    def _quote(codes, basic):
        names = dict(zip(basic["code"], basic["name"]))
        return pd.DataFrame({
            "code": list(codes),
            "name": [names.get(c, c) for c in codes],
            "last_price": [100.0] * len(codes),
            "prev_close_price": [98.0] * len(codes),
            "turnover": [5e9] * len(codes),
            "turnover_rate": [0.5] * len(codes),
            "volume": [1e7] * len(codes),
        })

    def _install_fake(self, provider, basic):
        requested = []

        def fake_call(method, *a, **k):
            if method == "get_stock_basicinfo":
                return basic
            self.assertEqual(method, "get_market_snapshot")
            codes = list(a[0])
            requested.append(codes)
            hit = [c for c in codes if c in (self.OTC, self.NO_RIGHT)]
            if hit:
                raise RuntimeError(
                    f"Futu get_market_snapshot 调用失败：暂不提供美股 OTC 市场行情 {hit[0][3:]}"
                )
            return self._quote(codes, basic)

        provider._call = fake_call
        return requested

    def test_otc_filtered_at_source_and_batch_recovers(self):
        """OTC 被源头剔除，残留的无权限标的由二分容错隔离，扫描不再中断。"""
        provider = FutuProvider(request_interval=0.0, snapshot_batch_size=2)
        requested = self._install_fake(provider, self._basicinfo())

        df = provider.get_us_spot()

        self.assertEqual(list(df["code"]), ["AAPL", "MSFT", "NVDA"])
        self.assertEqual(df.attrs["otc_excluded"], 1)
        self.assertEqual(len(df.attrs["snapshot_skipped"]), 1)
        self.assertIn("BADQ", df.attrs["snapshot_skipped"][0])
        self.assertFalse(any(self.OTC in chunk for chunk in requested))
        self.assertTrue(any(len(chunk) == 1 for chunk in requested))  # 触发过二分
        self.assertEqual(provider._spot_market, "US")

    def test_no_exchange_type_column_falls_back_to_recovery(self):
        """旧版 SDK 无 exchange_type 列时不过滤，坏标的仍由批次容错隔离。"""
        provider = FutuProvider(request_interval=0.0, snapshot_batch_size=2)
        self._install_fake(provider, self._basicinfo(with_exchange_type=False))

        df = provider.get_us_spot()

        self.assertEqual(df.attrs["otc_excluded"], 0)
        self.assertEqual(list(df["code"]), ["AAPL", "MSFT", "NVDA"])
        self.assertEqual(len(df.attrs["snapshot_skipped"]), 2)

    def test_exclude_otc_can_be_disabled(self):
        """US_EXCLUDE_OTC=false 时保留 OTC 代码，靠批次容错兜底。"""
        provider = FutuProvider(
            request_interval=0.0, snapshot_batch_size=2, us_exclude_otc=False
        )
        requested = self._install_fake(provider, self._basicinfo())

        df = provider.get_us_spot()

        self.assertEqual(df.attrs["otc_excluded"], 0)
        self.assertTrue(any(self.OTC in chunk for chunk in requested))
        self.assertEqual(list(df["code"]), ["AAPL", "MSFT", "NVDA"])

    def test_all_batches_fail_raises_clear_error(self):
        """全部标的都无行情权限时抛出带排查方向的明确错误，而非裸 traceback。"""
        provider = FutuProvider(request_interval=0.0, snapshot_batch_size=2)
        basic = self._basicinfo()

        def fake_call(method, *a, **k):
            if method == "get_stock_basicinfo":
                return basic
            raise RuntimeError("暂不提供美股 OTC 市场行情 2578256D")

        provider._call = fake_call
        with self.assertRaises(RuntimeError) as ctx:
            provider.get_us_spot()
        self.assertIn("行情权限", str(ctx.exception))

    def test_a_share_get_spot_uses_same_recovery(self):
        """A 股快照复用同一容错逻辑：个别坏标的不再拖垮整批。"""
        provider = FutuProvider(request_interval=0.0, snapshot_batch_size=2)
        basic = pd.DataFrame({
            "code": ["SH.600519", "SH.600001"],
            "name": ["贵州茅台", "无权限"],
        })

        def fake_call(method, *a, **k):
            if method == "get_stock_basicinfo":
                return basic
            codes = list(a[0])
            if "SH.600001" in codes:
                raise RuntimeError("无行情权限")
            return self._quote(codes, pd.DataFrame({
                "code": ["SH.600519", "SH.600001"],
                "name": ["贵州茅台", "无权限"],
            }))

        provider._call = fake_call
        df = provider.get_spot()
        self.assertEqual(list(df["code"]), ["600519"])
        self.assertEqual(provider._spot_market, "SH")

    def test_scan_reports_otc_and_skipped_notice(self):
        """编排层把容错统计转换成界面提示，用户能看到扫描仍在继续。"""
        class NoticeProvider(FakeUSProvider):
            def get_us_spot(self, symbols=None):
                df = _us_spot()
                df.attrs["otc_excluded"] = 3
                df.attrs["snapshot_skipped"] = ["US.BADQ（无行情权限）"]
                return df

        messages = []
        result = USMarketOrchestrator(NoticeProvider()).scan(message=messages.append)
        self.assertTrue(any("已过滤 3 个 OTC" in m for m in messages))
        self.assertTrue(any("自动跳过" in m for m in messages))
        self.assertEqual(result["market"], "US")


if __name__ == "__main__":
    unittest.main(verbosity=2)
