"""调试日志体系离线测试。

覆盖：配置幂等、滚动文件落盘、级别过滤、GUI 回调转发、
log_duration / log_df 工具，以及 FutuProvider._call 的关键埋点。
每个用例都会恢复 root logger 状态，避免影响其它测试模块。
"""

import logging
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import futu as ft

from ashare_hunter.data.futu_provider import FutuProvider
from ashare_hunter.utils.logging import (
    CallbackLogHandler, NOISY_LOGGERS, current_log_file,
    log_df, log_duration, setup_logging, shutdown_logging, to_level,
)


class LoggingSetupTest(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self.origin_handlers = list(self.root.handlers)
        self.origin_level = self.root.level
        self.origin_disable = self.root.manager.disable
        self.origin_noisy = {n: logging.getLogger(n).level for n in NOISY_LOGGERS}
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = str(Path(self.tmp.name) / "logs" / "test.log")

    def tearDown(self):
        for handler in list(self.root.handlers):
            self.root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        for handler in self.origin_handlers:
            self.root.addHandler(handler)
        self.root.setLevel(self.origin_level)
        self.root.manager.disable = self.origin_disable
        for name, level in self.origin_noisy.items():
            logging.getLogger(name).setLevel(level)
        self.tmp.cleanup()

    @staticmethod
    def _flush():
        for handler in logging.getLogger().handlers:
            handler.flush()

    def _setup(self, **kwargs):
        options = dict(
            level="DEBUG", log_file=self.log_path, console_level="CRITICAL",
            gui_level="CRITICAL", third_party_level="WARNING",
        )
        options.update(kwargs)
        return setup_logging(**options)

    def test_setup_is_idempotent(self):
        """重复初始化只替换自身 handler，不叠加输出。"""
        self._setup()
        first = len(self.root.handlers)
        self._setup()
        self.assertEqual(len(self.root.handlers), first)
        self.assertEqual(first, 2)  # 控制台 + 滚动文件

    def test_debug_written_to_rotating_file(self):
        self._setup()
        logging.getLogger("investmind.check").debug("调试标记 %s", "ABC123")
        self._flush()
        content = Path(self.log_path).read_text(encoding="utf-8")
        self.assertIn("ABC123", content)
        self.assertIn("investmind.check", content)
        self.assertIn("[DEBUG", content)

    def test_info_level_filters_debug(self):
        self._setup(level="INFO")
        probe = logging.getLogger("investmind.check")
        probe.debug("DEBUG_SHOULD_NOT_APPEAR")
        probe.info("INFO_SHOULD_APPEAR")
        self._flush()
        content = Path(self.log_path).read_text(encoding="utf-8")
        self.assertNotIn("DEBUG_SHOULD_NOT_APPEAR", content)
        self.assertIn("INFO_SHOULD_APPEAR", content)

    def test_gui_callback_receives_only_allowed_levels(self):
        lines = []
        self._setup(gui_level="INFO", gui_callback=lines.append)
        probe = logging.getLogger("investmind.check")
        probe.debug("DEBUG_ONLY_MARK")
        probe.info("INFO_ONLY_MARK")
        self.assertFalse(any("DEBUG_ONLY_MARK" in line for line in lines))
        self.assertTrue(any("INFO_ONLY_MARK" in line for line in lines))
        self.assertTrue(any("[INFO]" in line for line in lines))

    def test_current_log_file(self):
        self._setup()
        self.assertEqual(Path(current_log_file()).resolve(), Path(self.log_path).resolve())

    def test_shutdown_releases_file_handle(self):
        """主动关闭后可删除日志文件（Windows 文件占用验证）。"""
        self._setup()
        self.assertIsNotNone(current_log_file())
        shutdown_logging()
        self.assertIsNone(current_log_file())
        self.assertTrue(Path(self.log_path).exists())
        Path(self.log_path).unlink()  # 不再被进程占用

    def test_to_level(self):
        self.assertEqual(to_level("debug"), logging.DEBUG)
        self.assertEqual(to_level(30), 30)
        self.assertEqual(to_level("无效级别"), logging.INFO)


class LoggingHelperTest(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.logger = logging.getLogger("investmind.helper.test")
        self.old_level = self.logger.level
        self.old_propagate = self.logger.propagate
        self.handler = CallbackLogHandler(self.lines.append, level=logging.DEBUG)
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)
        self.logger.propagate = self.old_propagate

    def test_log_duration_success(self):
        with log_duration(self.logger, "阶段X", level=logging.DEBUG):
            pass
        self.assertTrue(any("阶段X 开始" in line for line in self.lines))
        self.assertTrue(any("阶段X 完成" in line for line in self.lines))

    def test_log_duration_failure_records_traceback_and_reraises(self):
        with self.assertRaises(ValueError):
            with log_duration(self.logger, "阶段Y", level=logging.DEBUG):
                raise ValueError("boom-marker")
        self.assertTrue(any("阶段Y 失败" in line for line in self.lines))
        self.assertTrue(any("boom-marker" in line for line in self.lines))

    def test_log_df_reports_shape_without_full_dump(self):
        frame = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        log_df(self.logger, "测试表", frame, level=logging.DEBUG)
        self.assertTrue(any("shape=(3, 2)" in line for line in self.lines))
        self.assertTrue(any("测试表" in line for line in self.lines))


class FutuCallLoggingTest(unittest.TestCase):
    """验证数据层统一入口 _call 记录了方法名、耗时、返回规模与失败原因。"""

    def setUp(self):
        self.lines = []
        self.logger = logging.getLogger("ashare_hunter.data.futu_provider")
        self.old_level = self.logger.level
        self.old_propagate = self.logger.propagate
        self.handler = CallbackLogHandler(self.lines.append, level=logging.DEBUG)
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

        self.provider = FutuProvider(request_interval=0.0)

        class FakeCtx:
            def get_market_snapshot(self, codes):
                return ft.RET_OK, pd.DataFrame(
                    {"code": codes, "last_price": [1.0] * len(codes)}
                )

            def bad_method(self, *args, **kwargs):
                return ft.RET_ERROR, "暂不提供美股 OTC 市场行情 2578256D"

        self.provider._ctx = lambda: FakeCtx()
        self.provider._pace = lambda: None

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)
        self.logger.propagate = self.old_propagate

    def test_success_logs_method_timing_and_shape(self):
        self.provider._call("get_market_snapshot", ["US.AAPL"])
        text = "\n".join(self.lines)
        self.assertIn("Futu 请求 get_market_snapshot", text)
        self.assertIn("Futu 响应 get_market_snapshot", text)
        self.assertIn("耗时", text)
        self.assertIn("DataFrame", text)

    def test_failure_logs_error_reason(self):
        with self.assertRaises(RuntimeError):
            self.provider._call("bad_method", ["US.XXX"])
        text = "\n".join(self.lines)
        self.assertIn("返回失败", text)
        self.assertIn("OTC", text)
        self.assertIn("2578256D", text)

    def test_long_code_list_is_summarised_not_dumped(self):
        codes = [f"US.T{i:04d}" for i in range(400)]
        self.provider._call("get_market_snapshot", codes)
        request_line = next(x for x in self.lines if "Futu 请求" in x)
        self.assertIn("list[400]", request_line)
        self.assertNotIn("US.T0200", request_line)  # 中间代码不应被完整打印


if __name__ == "__main__":
    unittest.main(verbosity=2)
