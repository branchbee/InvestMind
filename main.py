import logging
import time

from ashare_hunter.utils.logging import setup_logging, current_log_file
from config import settings

logger = logging.getLogger(__name__)


def main():
    setup_logging()
    started = time.perf_counter()
    logger.info("CLI 扫描开始，日志文件：%s，A股最低总市值：%.0f 元",
                current_log_file(), settings.a_min_market_cap_cny)
    # 延迟导入：langgraph 在部分环境有依赖限制，放在日志初始化之后便于排查
    from ashare_hunter.graph.workflow import build_workflow
    result = build_workflow().invoke({})
    print("\n===== A-Share Hunter 扫描完成 =====")
    print("扫描时间:", result.get("scan_time"))
    print("市场环境:", result.get("market_summary", {}).get("market_regime"))
    print("强势板块:", len(result.get("top_sectors", [])))
    print("候选股票:", len(result.get("top_stocks", [])))
    print("报告:", result.get("report_path"))
    cap = result.get("market_cap_filter") or {}
    if cap.get("enabled"):
        print(f"市值门槛: {cap.get('threshold', 0) / 1e8:.1f} 亿元；"
              f"过滤前 {cap.get('before', 0)} 只 → 保留 {cap.get('after', 0)} 只"
              f"（低市值剔除 {cap.get('below', 0)} 只）")
        if not cap.get("has_field"):
            print("注意: 快照未提供 total_market_val，市值门槛未实际生效。")
    print("调试日志:", current_log_file())
    logger.info("CLI 扫描完成，耗时 %.3fs", time.perf_counter() - started)


if __name__ == "__main__":
    main()
