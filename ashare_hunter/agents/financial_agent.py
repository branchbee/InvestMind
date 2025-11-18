"""财务 Agent：ROE、营收增长、净利润、毛利率。

所有指标来自 Futu get_financials_statements 的真实财报字段；
接口未返回或字段无法核验时，明确标记"数据不足"，不生成虚构数字。
"""

from .fundamental_base import (
    pick_metric, compute_yoy, score_by_thresholds, score_label,
    fmt_pct, fmt_num,
)

# 字段别名：覆盖中英文常见口径；"归母净利润"优先于"净利润"以避免口径混淆。
ALIASES = {
    "roe": ["净资产收益率", "roe", "returnon Equity", "returnonequity", "加权净资产收益率", "净资产收益率roe"],
    "revenue": ["营业总收入", "营业收入", "totalrevenue", "revenue", "operatingrevenue", "主营收入"],
    "net_profit": [
        "归属于母公司股东的净利润", "归属于母公司所有者的净利润", "归母净利润",
        "netprofitattributabletoparent", "netprofit", "净利润",
    ],
    "gross_margin": ["销售毛利率", "毛利率", "grossprofitmargin", "grossmargin", "毛利率"],
}


class FinancialAgent:
    """解析财报中的 ROE / 营收 / 净利润 / 毛利率，并输出规则化评分。"""

    def run(self, key_indicators=None, income_statement=None):
        key_reports = self._reports(key_indicators)
        income_reports = self._reports(income_statement)
        if not key_reports and not income_reports:
            return {
                "agent": "财务 Agent", "status": "数据不足",
                "conclusion": "Futu 未返回可核验的财报数据。ROE、营收增速、净利润、毛利率均不作判断，不生成虚构数值。",
                "score": None, "dimensions": {},
                "roe_pct": None, "revenue_yoy_pct": None,
                "net_profit_yoy_pct": None, "gross_margin_pct": None,
            }

        # ROE 与毛利率优先取关键指标表，回退到利润表
        roe = pick_metric(key_reports, ALIASES["roe"]) or pick_metric(income_reports, ALIASES["roe"])
        gross = pick_metric(key_reports, ALIASES["gross_margin"]) or pick_metric(income_reports, ALIASES["gross_margin"])
        revenue = pick_metric(income_reports, ALIASES["revenue"]) or pick_metric(key_reports, ALIASES["revenue"])
        profit = pick_metric(income_reports, ALIASES["net_profit"]) or pick_metric(key_reports, ALIASES["net_profit"])

        revenue_yoy = (revenue or {}).get("yoy")
        if revenue is not None and revenue_yoy is None:
            calc = compute_yoy(income_reports or key_reports, ALIASES["revenue"])
            revenue_yoy = calc["yoy"] if calc else None
        profit_yoy = (profit or {}).get("yoy")
        if profit is not None and profit_yoy is None:
            calc = compute_yoy(income_reports or key_reports, ALIASES["net_profit"])
            profit_yoy = calc["yoy"] if calc else None

        roe_value = (roe or {}).get("value")
        gross_value = (gross or {}).get("value")

        # 阈值：ROE≥15% 优、≥8% 良；营收/净利增速≥20% 强、≥0 中性区间；毛利率≥30% 较优（行业差异大）
        scores = {
            "roe": score_by_thresholds(roe_value, [(15, 90), (8, 70), (0, 50), (-1e9, 25)]),
            "revenue_growth": score_by_thresholds(revenue_yoy, [(20, 85), (10, 70), (0, 55), (-1e9, 30)]),
            "profit_growth": score_by_thresholds(profit_yoy, [(20, 85), (10, 70), (0, 55), (-1e9, 25)]),
            "gross_margin": score_by_thresholds(gross_value, [(30, 80), (20, 65), (10, 50), (-1e9, 35)]),
        }
        available = [v for v in scores.values() if v is not None]
        score = round(sum(available) / len(available), 1) if available else None

        dimensions = {
            "roe": {
                "value": roe_value, "yoy": (roe or {}).get("yoy"),
                "period": (roe or {}).get("period"), "field": (roe or {}).get("field"),
                "score": scores["roe"], "label": score_label(scores["roe"]),
            },
            "revenue": {
                "value": (revenue or {}).get("value"), "yoy": revenue_yoy,
                "period": (revenue or {}).get("period"), "field": (revenue or {}).get("field"),
                "score": scores["revenue_growth"], "label": score_label(scores["revenue_growth"]),
            },
            "net_profit": {
                "value": (profit or {}).get("value"), "yoy": profit_yoy,
                "period": (profit or {}).get("period"), "field": (profit or {}).get("field"),
                "score": scores["profit_growth"], "label": score_label(scores["profit_growth"]),
            },
            "gross_margin": {
                "value": gross_value, "yoy": (gross or {}).get("yoy"),
                "period": (gross or {}).get("period"), "field": (gross or {}).get("field"),
                "score": scores["gross_margin"], "label": score_label(scores["gross_margin"]),
            },
        }

        parts = [
            f"ROE {fmt_pct(roe_value)}（{score_label(scores['roe'])}）",
            f"营收同比 {fmt_pct(revenue_yoy)}（{score_label(scores['revenue_growth'])}）",
            f"净利润同比 {fmt_pct(profit_yoy)}（{score_label(scores['profit_growth'])}）",
            f"毛利率 {fmt_pct(gross_value)}（{score_label(scores['gross_margin'])}）",
        ]
        periods = sorted({d["period"] for d in dimensions.values() if d["period"] and d["period"] != "未知报告期"})
        conclusion = "；".join(parts) + "。"
        if periods:
            conclusion += f" 数据报告期：{', '.join(periods)}（来源：Futu 财报接口）。"
        missing = [name for name, d in dimensions.items() if d["value"] is None and d["yoy"] is None]
        if missing:
            conclusion += " 未取得字段：" + "、".join(missing) + "。"
        conclusion += " 毛利率阈值未做行业区分，跨行业比较需谨慎。"

        return {
            "agent": "财务 Agent", "status": "完成" if score is not None else "数据不足",
            "score": score, "dimensions": dimensions,
            "roe_pct": roe_value, "revenue_yoy_pct": revenue_yoy,
            "net_profit_yoy_pct": profit_yoy, "gross_margin_pct": gross_value,
            "conclusion": conclusion,
        }

    @staticmethod
    def _reports(bundle):
        if not isinstance(bundle, dict) or not bundle.get("available"):
            return []
        return bundle.get("reports") or []
