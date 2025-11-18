from datetime import datetime
from pathlib import Path

class ReportAgent:
    def save(self, market, sectors, stocks, explanation, scan_time):
        out = Path("output"); out.mkdir(exist_ok=True)
        day = datetime.now().strftime("%Y%m%d")
        sp, tp, rp = out/f"sectors_{day}.csv", out/f"stocks_{day}.csv", out/f"report_{day}.md"
        sectors.to_csv(sp, index=False, encoding="utf-8-sig")
        stocks.to_csv(tp, index=False, encoding="utf-8-sig")
        lines = ["# A-Share Hunter 扫描报告", "", f"- 扫描时间：{scan_time}",
            f"- 市场环境：{market.get('market_regime')}",
            f"- 上涨/下跌家数：{market.get('up_count')}/{market.get('down_count')}",
            f"- 上涨家数占比：{market.get('advance_decline_ratio', 0):.1%}",
            f"- 快照成交额合计：{market.get('total_amount_yuan', 0):,.0f} 元", "",
            "## 强势板块", "", sectors.head(20).to_markdown(index=False) if not sectors.empty else "无符合条件板块",
            "", "## 强势个股", "", stocks.head(30).to_markdown(index=False) if not stocks.empty else "无符合条件个股",
            "", "## 研究解读", "", explanation, "",
            "## 风险提示", "", "数据可能延迟或缺失；本报告仅供研究筛选，不构成投资建议。交易前请核验公告、财报、停复牌、涨跌停及流动性。"]
        rp.write_text("\n".join(lines), encoding="utf-8")
        return str(rp)
