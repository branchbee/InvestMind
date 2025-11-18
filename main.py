from ashare_hunter.graph.workflow import build_workflow
from ashare_hunter.utils.logging import setup_logging

def main():
    setup_logging()
    result = build_workflow().invoke({})
    print("\n===== A-Share Hunter 扫描完成 =====")
    print("扫描时间:", result.get("scan_time"))
    print("市场环境:", result.get("market_summary", {}).get("market_regime"))
    print("强势板块:", len(result.get("top_sectors", [])))
    print("候选股票:", len(result.get("top_stocks", [])))
    print("报告:", result.get("report_path"))

if __name__ == "__main__":
    main()
