"""GUI 市场下拉框切换离线测试。

验证：移除独立美股 Tab 与观察池后，通过顶部下拉框在 A 股 / 美股间切换，
两个市场共用强势板块 Tab，界面文案、美元单位与结果回填均正确。
不依赖 Futu OpenD；Qt 以 offscreen 模式运行。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
from PySide6.QtWidgets import QApplication

from gui import (
    HunterWindow, MarketScanWorker, TradingAnalysisWorker,
    MARKET_A, MARKET_US,
)

# 单个 QApplication 实例，供全部用例共享
APP = QApplication.instance() or QApplication([])


def _us_result():
    return {
        "market": "US",
        "currency": "USD",
        "watchlist": ["AAPL", "NVDA", "MSFT"],
        "scan_time": "2026-11-18T20:00:00+08:00",
        "market_summary": {
            "market_regime": "偏强", "advance_decline_ratio": 0.75,
            "up_count": 3, "down_count": 1, "flat_count": 0,
            "avg_change_pct": 1.5, "total_amount_yuan": 8e9,
            "amount_note": "成交额为美元",
        },
        "top_sectors": pd.DataFrame(),
        "top_stocks": pd.DataFrame([{
            "code": "AAPL", "name": "Apple", "change_pct": 2.1,
            "amount": 5e9, "score": 80.0,
        }]),
        "explanation": "美股板块研究解读",
    }


class MarketComboTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.window = HunterWindow()

    def setUp(self):
        self.window.market_combo.setCurrentIndex(0)  # 每个用例从 A 股开始

    # --- 结构 ---
    def test_no_standalone_us_tab(self):
        labels = [self.window.tabs.tabText(i) for i in range(self.window.tabs.count())]
        self.assertNotIn("美股研究", labels)
        self.assertEqual(len(labels), 5)

    def test_combo_has_two_markets(self):
        items = [
            (self.window.market_combo.itemText(i), self.window.market_combo.itemData(i))
            for i in range(self.window.market_combo.count())
        ]
        self.assertEqual(items, [
            ("A股（沪深）", MARKET_A), ("美股（全市场）", MARKET_US)
        ])

    # --- 切换行为 ---
    def test_no_watchlist_input(self):
        """观察池功能已移除：界面不应再有观察池输入框。"""
        self.assertFalse(hasattr(self.window, "pool_row"))
        self.assertFalse(hasattr(self.window, "watchlist_input"))

    def test_default_a_share_state(self):
        self.assertFalse(self.window.is_us())
        self.assertTrue(self.window.tabs.isTabVisible(self.window.sector_tab_index))
        self.assertIn("A股", self.window.scan_button.text())

    def test_switch_to_us_keeps_sector_tab(self):
        self.window.market_combo.setCurrentIndex(1)
        self.assertTrue(self.window.is_us())
        self.assertTrue(self.window.tabs.isTabVisible(self.window.sector_tab_index))  # 美股也有强势板块
        self.assertIn("美股", self.window.scan_button.text())
        self.assertEqual(self.window.card_amount.hint.text(), "美元口径汇总")
        self.assertEqual(self.window.card_sector.title.text(), "强势板块")

    def test_switch_back_to_a_share(self):
        self.window.market_combo.setCurrentIndex(1)
        self.window.market_combo.setCurrentIndex(0)
        self.assertTrue(self.window.tabs.isTabVisible(self.window.sector_tab_index))
        self.assertFalse(self.window.analyze_button.isEnabled())

    def test_switching_clears_previous_result(self):
        self.window.market_combo.setCurrentIndex(1)
        self.window.on_completed(_us_result())
        self.assertIsNotNone(self.window.result)
        self.window.market_combo.setCurrentIndex(0)
        self.assertIsNone(self.window.result)
        self.assertEqual(self.window.stock_table.rowCount(), 0)
        self.assertFalse(self.window.analyze_button.isEnabled())

    # --- 美股结果回填 ---
    def test_us_result_use_dollar_unit(self):
        self.window.market_combo.setCurrentIndex(1)
        self.window.on_completed(_us_result())
        self.assertEqual(self.window.card_amount.value.text(), "80.00 亿美元")
        self.assertEqual(self.window.card_sector.value.text(), "0")   # 美股结果未含板块则显示 0
        self.assertEqual(self.window.card_stock.value.text(), "1")
        headers = [
            self.window.stock_table.horizontalHeaderItem(i).text()
            for i in range(self.window.stock_table.columnCount())
        ]
        amt_col = headers.index("amount")
        self.assertIn("亿美元", self.window.stock_table.item(0, amt_col).text())
        self.assertTrue(self.window.analyze_button.isEnabled())

    def test_us_sector_table_filled(self):
        """美股扫描结果含板块时，强势板块表格同步填充。"""
        result = _us_result()
        result["top_sectors"] = pd.DataFrame([{
            "sector_name": "科技", "board_type": "industry", "score": 88.0,
            "change_pct": 2.1, "up_count": 3, "down_count": 1, "amount": 1.7e10,
        }])
        self.window.market_combo.setCurrentIndex(1)
        self.window.on_completed(result)
        self.assertEqual(self.window.sector_table.rowCount(), 1)
        self.assertEqual(self.window.card_sector.value.text(), "1")

    # --- Worker 分派 ---
    def test_scan_worker_carries_market(self):
        a = MarketScanWorker(market=MARKET_A)
        u = MarketScanWorker(market=MARKET_US)
        self.assertEqual(a.market, MARKET_A)
        self.assertEqual(u.market, MARKET_US)
        self.assertFalse(hasattr(u, "symbols"))  # 观察池参数已移除

    def test_analysis_worker_carries_market(self):
        worker = TradingAnalysisWorker(
            {"code": "AAPL", "name": "Apple"}, {}, pd.DataFrame(), market=MARKET_US
        )
        self.assertEqual(worker.market, MARKET_US)
        self.assertEqual(worker.stock["code"], "AAPL")

if __name__ == "__main__":
    unittest.main(verbosity=2)
