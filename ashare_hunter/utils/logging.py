"""统一调试日志配置。

- 控制台：默认 INFO，避免 DEBUG 级别刷屏
- 滚动文件：按 LOG_LEVEL 记录全量（设 DEBUG 后包含每次 Futu 调用、阶段耗时、堆栈）
- GUI「运行日志」面板：通过回调转发（配合 Qt Signal.emit，跨线程安全）
- 幂等：重复调用只会替换本模块添加的 handler，不会叠加输出

用法::

    from ashare_hunter.utils.logging import setup_logging, log_duration, log_df
    setup_logging()                 # 从 config.settings 读配置
    setup_logging(level="DEBUG")    # 临时开启详细调试
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

_MARKER = "_investmind_handler"
DETAIL_FMT = (
    "%(asctime)s.%(msecs)03d [%(levelname)-7s] %(name)s:%(lineno)d "
    "[%(threadName)s] - %(message)s"
)
SIMPLE_FMT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
DATE_FMT = "%Y-%m-%d %H:%M:%S"

# 第三方库在 DEBUG 下输出量极大，默认压到 WARNING；需要时调 LOG_THIRD_PARTY_LEVEL
NOISY_LOGGERS = (
    "futu", "urllib3", "requests", "httpx", "matplotlib", "asyncio",
    "langchain", "langchain_core", "langsmith", "openai",
)


def to_level(value) -> int:
    """把 'DEBUG' / logging.DEBUG / 10 统一成整数级别，非法值回退 INFO。"""
    if isinstance(value, int):
        return value
    level = getattr(logging, str(value).strip().upper(), None)
    return level if isinstance(level, int) else logging.INFO


class CallbackLogHandler(logging.Handler):
    """把格式化后的日志交给回调。

    GUI 传入 ``Signal.emit`` 即可：Qt 会自动把跨线程调用排队到主线程，
    避免 Worker 线程直接操作 QTextEdit 造成的线程安全问题。
    """

    def __init__(self, callback: Callable[[str], None], level=logging.NOTSET):
        super().__init__(level)
        self._callback = callback
        self.setFormatter(logging.Formatter(SIMPLE_FMT, datefmt=DATE_FMT))
        setattr(self, _MARKER, True)

    def emit(self, record):
        try:
            self._callback(self.format(record))
        except Exception:
            self.handleError(record)


def setup_logging(
    level=None,
    log_file=None,
    console_level=None,
    gui_level=None,
    third_party_level=None,
    max_bytes=None,
    backup_count=None,
    gui_callback: Optional[Callable[[str], None]] = None,
    force: bool = False,
):
    """初始化 root logger。所有参数留空时从 ``config.settings`` 读取。

    返回 root logger；可重复调用（用于测试或运行时改级别），不会重复添加 handler。
    """
    from config import settings  # 延迟导入：config 不依赖本模块，避免循环引用

    level_i = to_level(level if level is not None else settings.log_level)
    console_i = to_level(console_level if console_level is not None else settings.log_console_level)
    gui_i = to_level(gui_level if gui_level is not None else settings.log_gui_level)
    noisy_i = to_level(third_party_level if third_party_level is not None else settings.log_third_party_level)
    file_path = Path(log_file if log_file is not None else settings.log_file)
    max_bytes = int(max_bytes if max_bytes is not None else settings.log_max_bytes)
    backup_count = int(backup_count if backup_count is not None else settings.log_backup_count)

    root = logging.getLogger()
    # 只清理本模块装过的 handler（force=True 时清理全部，含 basicConfig 的默认 handler）
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
    else:
        shutdown_logging()
    root.setLevel(min(level_i, console_i, gui_i))

    simple = logging.Formatter(SIMPLE_FMT, datefmt=DATE_FMT)
    detail = logging.Formatter(DETAIL_FMT, datefmt=DATE_FMT)

    console = logging.StreamHandler()
    console.setLevel(console_i)
    console.setFormatter(simple)
    setattr(console, _MARKER, True)
    root.addHandler(console)

    file_ok = False
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(
            file_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        rotating.setLevel(level_i)
        rotating.setFormatter(detail)
        setattr(rotating, _MARKER, True)
        root.addHandler(rotating)
        file_ok = True
    except OSError as exc:
        # 日志文件不可写不应阻断程序，控制台仍可用
        logging.getLogger(__name__).warning("日志文件不可用 %s：%s", file_path, exc)

    if gui_callback is not None:
        root.addHandler(CallbackLogHandler(gui_callback, level=gui_i))

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_i)

    logging.getLogger(__name__).info(
        "日志已初始化：文件级别=%s 控制台=%s GUI=%s 第三方=%s 文件=%s",
        logging.getLevelName(level_i), logging.getLevelName(console_i),
        "启用" if gui_callback is not None else "未启用",
        logging.getLevelName(noisy_i),
        str(file_path) if file_ok else "不可用",
    )
    return root


def shutdown_logging():
    """关闭并移除本模块创建的 handler，立即释放日志文件句柄。

    程序退出时标准库 logging.shutdown 也会清理；此函数用于测试或运行时
    重新配置日志前主动释放（Windows 上文件被占用会妨碍删除/重建）。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MARKER, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def current_log_file() -> Optional[str]:
    """返回当前滚动日志文件路径，便于在 CLI/GUI 中提示用户去哪看日志。"""
    for handler in logging.getLogger().handlers:
        if getattr(handler, _MARKER, False) and isinstance(handler, RotatingFileHandler):
            return str(handler.baseFilename)
    return None


@contextmanager
def log_duration(logger, label: str, level: int = logging.INFO):
    """记录阶段开始 / 结束耗时；异常时带堆栈记录并原样抛出。"""
    started = time.perf_counter()
    logger.log(level, "%s 开始", label)
    try:
        yield
    except Exception:
        logger.error("%s 失败（耗时 %.3fs）", label, time.perf_counter() - started, exc_info=True)
        raise
    else:
        logger.log(level, "%s 完成，耗时 %.3fs", label, time.perf_counter() - started)


def log_df(logger, label: str, df, level: int = logging.DEBUG, sample: int = 3):
    """以 DEBUG 输出 DataFrame 的规模、列名与少量样本，不打印全表。"""
    if not logger.isEnabledFor(level):
        return
    if df is None:
        logger.log(level, "%s: None", label)
        return
    shape = getattr(df, "shape", None)
    if shape is None:
        logger.log(level, "%s: %s", label, type(df).__name__)
        return
    columns = list(getattr(df, "columns", []))[:12]
    logger.log(level, "%s: shape=%s columns=%s", label, tuple(shape), columns)
    if sample and shape[0]:
        try:
            logger.log(level, "%s 前 %s 行：\n%s", label, sample,
                       df.head(sample).to_string(index=False, max_cols=8))
        except Exception:
            pass
