import logging
import time

from ashare_hunter.utils.logging import setup_logging, current_log_file

logger = logging.getLogger(__name__)


def main():
    setup_logging()
    started = time.perf_counter()
    logger.info("CLI 扫描开始，日志文件：%s", current_log_file())
    # 延迟导入：langgraph 在部分环境有依赖限制，放在日志初始化之后便于排查
    from ashare_hunter.graph.workflow import build_workflow
    result = build_workflow().invoke({})
    print("\n===== A-Share Hunter 扫描完成 =====")
    print("扫描时间:", result.get("scan_time"))
    print("市场环境:", result.get("market_summary", {}).get("market_regime"))
    print("强势板块:", len(result.get("top_sectors", [])))
    print("候选股票:", len(result.get("top_stocks", [])))
    print("报告:", result.get("report_path"))
    print("调试日志:", current_log_file())
    logger.info("CLI 扫描完成，耗时 %.3fs", time.perf_counter() - started)


if __name__ == "__main__":
    main()
