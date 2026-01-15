"""深度研究按钮（analyze_button）的视觉与交互状态机离线测试。

覆盖：专属样式 / 五态流转 / 选中联动 / 自动选首只 / 研究中计时反馈 /
防重复启动 Worker / 线程结束后解锁 / 结果弹窗复用 / 市值二次防护。
不依赖 Futu OpenD，Qt 以 offscreen 模式运行；所有 Worker 均为替身。
"""

import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LLM_ENABLED", "false")

import pandas as pd
from PySide6.QtWidgets import QApplication

import gui
from gui import HunterWindow

APP = QApplication.instance() or QApplication([])


class _Signal:
    """最小 Signal 替身：只需要 connect 与 emit。"""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class _FakeWorker:
    created = 0

    def __init__(self, *args, **kwargs):
        _FakeWorker.created += 1
        self.completed = _Signal()
        self.failed = _Signal()
        self.message = _Signal()
        self.finished = _Signal()
        self.started = False

    def start(self):
        self.started = True

    def isRunning(self):
        return self.started

    def deleteLater(self):
        pass


def _result(rows=None, cap_filter=None):
    if rows is None:
        rows = [
            {"code": "SH.600001", "name": "测试A", "change_pct": 1.1, "score": 80.0},
            {"code": "SH.600002", "name": "测试B", "change_pct": 2.1, "score": 90.0},
        ]
    result = {
        "scan_time": "2026-11-19T00:00:00+08:00",
        "market_summary": {
            "market_regime": "偏强", "advance_decline_ratio": 0.75,
            "up_count": 2, "down_count": 1, "flat_count": 0,
            "avg_change_pct": 1.2, "total_amount_yuan": 1e9,
        },
        "top_sectors": pd.DataFrame(),
        "top_stocks": pd.DataFrame(rows),
        "explanation": "研究解读",
    }
    if cap_filter:
        result["market_cap_filter"] = cap_filter
    return result


class AnalyzeButtonTest(unittest.TestCase):
    def setUp(self):
        _FakeWorker.created = 0
        self.window = HunterWindow()

    def tearDown(self):
        self.window._analysis_timer.stop()
        if self.window.analysis_dialog is not None:
            self.window.analysis_dialog.close()
        self.window.close()

    def _ready(self, result=None):
        self.window.on_completed(result or _result())
        return self.window

    def _busy(self):
        """以替身 Worker 启动一次研究，返回窗口。"""
        with patch("gui.TradingAnalysisWorker", _FakeWorker):
            self.window.analyze_selected_stock()
        return self.window

    # --- 视觉 ---
    def test_dedicated_style_and_shortcuts(self):
        button = self.window.analyze_button
        sheet = self.window.styleSheet()
        self.assertEqual(button.objectName(), "analyzeButton")
        self.assertIn("QPushButton#analyzeButton", sheet)
        self.assertIn("QPushButton#analyzeButton:hover", sheet)
        self.assertIn("QPushButton#analyzeButton:disabled", sheet)
        self.assertIn('QPushButton#analyzeButton[state="busy"]', sheet)
        self.assertGreater(button.minimumWidth(), 0)
        self.assertEqual(button.width(), button.minimumWidth())
        keys = [s.key().toString() for s in self.window._analyze_shortcuts]
        self.assertEqual(keys, ["Ctrl+Return", "Ctrl+Enter"])

    # --- 状态流转 ---
    def test_initial_idle_state(self):
        w = self.window
        self.assertEqual(w._analyze_state, "idle")
        self.assertFalse(w.analyze_button.isEnabled())
        self.assertIn("待扫描", w.analyze_button.text())
        self.assertIn("扫描", w.analyze_button.toolTip())
        self.assertIn("—", w.selection_hint.text())
        self.assertFalse(any(s.isEnabled() for s in w._analyze_shortcuts))

    def test_scanning_state(self):
        w = self.window
        with patch("gui.MarketScanWorker", _FakeWorker):
            w.start_scan()
        self.assertEqual(w._analyze_state, "scanning")
        self.assertFalse(w.analyze_button.isEnabled())
        self.assertIn("扫描中", w.analyze_button.text())
        self.assertFalse(w.market_combo.isEnabled())
        self.assertFalse(w.scan_button.isEnabled())

    def test_no_candidate_state(self):
        w = self.window
        w.on_completed(_result(rows=[]))
        self.assertEqual(w._analyze_state, "no_result")
        self.assertFalse(w.analyze_button.isEnabled())
        self.assertIn("无候选", w.analyze_button.text())
        self.assertTrue(w.market_combo.isEnabled())

    def test_ready_autoselects_first_and_shows_code(self):
        w = self._ready()
        self.assertEqual(w._analyze_state, "ready")
        self.assertTrue(w.analyze_button.isEnabled())
        self.assertEqual(w.stock_table.currentRow(), 0)
        self.assertIn("SH.600001", w.analyze_button.text())
        self.assertIn("测试A", w.selection_hint.text())
        self.assertTrue(all(s.isEnabled() for s in w._analyze_shortcuts))
        self.assertIn("Ctrl+Enter", w.analyze_button.toolTip())

    def test_selection_updates_button_label(self):
        w = self._ready()
        w.stock_table.selectRow(1)
        self.assertIn("SH.600002", w.analyze_button.text())
        self.assertIn("测试B", w.selection_hint.text())

    def test_market_switch_resets_button(self):
        w = self._ready()
        w.market_combo.setCurrentIndex(1)
        self.assertEqual(w._analyze_state, "idle")
        self.assertFalse(w.analyze_button.isEnabled())
        self.assertIsNone(w.result)

    def test_scan_failure_resets_button(self):
        w = self._ready()
        with patch("gui.QMessageBox.critical"):
            w.on_failed("some traceback")
        self.assertEqual(w._analyze_state, "idle")
        self.assertFalse(w.analyze_button.isEnabled())
        self.assertIn("待扫描", w.analyze_button.text())
        self.assertTrue(w.market_combo.isEnabled())
        self.assertTrue(w.scan_button.isEnabled())

    # --- 点击与研究过程 ---
    def test_click_without_selection_defaults_to_first(self):
        w = self._ready()
        w.stock_table.clearSelection()
        with patch("gui.TradingAnalysisWorker", _FakeWorker):
            w.analyze_selected_stock()
        self.assertEqual(_FakeWorker.created, 1)
        self.assertEqual(w.stock_table.currentRow(), 0)
        self.assertEqual(w._analyze_state, "busy")
        self.assertEqual(w.analyze_button.property("state"), "busy")
        self.assertIn("研究中", w.analyze_button.text())
        self.assertFalse(w.market_combo.isEnabled())
        self.assertFalse(w.scan_button.isEnabled())
        self.assertFalse(any(s.isEnabled() for s in w._analyze_shortcuts))

    def test_busy_blocks_duplicate_worker(self):
        w = self._ready()
        with patch("gui.TradingAnalysisWorker", _FakeWorker):
            w.analyze_selected_stock()
            w.analyze_selected_stock()          # 再点按钮
            w.analyze_selected_stock(1, 0)      # 模拟双击另一行
        self.assertEqual(_FakeWorker.created, 1)
        self.assertEqual(w._analyze_state, "busy")
        self.assertIn("进行中", w.statusBar().currentMessage())

    def test_progress_tick_shows_elapsed(self):
        w = self._ready()
        self._busy()
        w._analysis_started = time.perf_counter() - 3.0
        w._tick_analysis_progress()
        self.assertIn("研究中", w.analyze_button.text())
        self.assertIn("3s", w.analyze_button.text())

    def test_finished_restores_ready_and_clears_worker(self):
        w = self._ready()
        self._busy()
        w._on_analysis_finished()
        self.assertEqual(w._analyze_state, "ready")
        self.assertTrue(w.analyze_button.isEnabled())
        self.assertTrue(w.market_combo.isEnabled())
        self.assertTrue(w.scan_button.isEnabled())
        self.assertTrue(all(s.isEnabled() for s in w._analyze_shortcuts))
        self.assertIsNone(w.analysis_worker)
        self.assertFalse(w._analysis_timer.isActive())

    def test_finished_is_idempotent(self):
        w = self._ready()
        self._busy()
        w._on_analysis_finished()
        first_text = w.analyze_button.text()
        w._on_analysis_finished()
        self.assertEqual(w._analyze_state, "ready")
        self.assertEqual(w.analyze_button.text(), first_text)

    def test_finished_restores_controls_after_state_reset(self):
        """边缘路径：研究中状态被市场切换重置时，线程结束仍须解锁下拉框与扫描按钮。"""
        w = self._ready()
        self._busy()
        w.market_combo.setCurrentIndex(1)
        self.assertEqual(w._analyze_state, "idle")
        w._on_analysis_finished()
        self.assertTrue(w.market_combo.isEnabled())
        self.assertTrue(w.scan_button.isEnabled())
        self.assertIsNone(w.analysis_worker)

    def test_completed_shows_and_reuses_dialog(self):
        w = self._ready()
        self._busy()
        payload = {"stock": {"name": "测试A", "code": "SH.600001"}}
        w.on_stock_analysis_completed(payload)
        first = w.analysis_dialog
        self.assertIsNotNone(first)
        self.assertIn("测试A", first.windowTitle())
        w.on_stock_analysis_completed(payload)
        self.assertIsNot(w.analysis_dialog, first)
        self.assertFalse(first.isVisible())
        w._on_analysis_finished()

    def test_failure_message_does_not_leave_button_busy(self):
        w = self._ready()
        self._busy()
        with patch("gui.QMessageBox.critical") as critical:
            w.on_stock_analysis_failed("traceback tail")
        critical.assert_called_once()
        w._on_analysis_finished()
        self.assertEqual(w._analyze_state, "ready")
        self.assertTrue(w.analyze_button.isEnabled())

    def test_market_cap_guard_still_blocks_analysis(self):
        cap_filter = {
            "enabled": True, "threshold": 5e9, "market": "SH",
            "currency": "CNY", "missing_policy": "keep",
            "before": 2, "after": 0, "below": 2, "missing": 0, "missing_excluded": 0,
        }
        rows = [
            {"code": "SH.600001", "name": "小市值A", "market_cap": 1e9, "score": 80.0},
            {"code": "SH.600002", "name": "小市值B", "market_cap": 2e9, "score": 90.0},
        ]
        w = self._ready(_result(rows=rows, cap_filter=cap_filter))
        with patch("gui.QMessageBox.information") as info:
            with patch("gui.TradingAnalysisWorker", _FakeWorker):
                w.analyze_selected_stock()
        info.assert_called_once()
        self.assertEqual(_FakeWorker.created, 0)
        self.assertEqual(w._analyze_state, "ready")


if __name__ == "__main__":
    unittest.main(verbosity=2)
