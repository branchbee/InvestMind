"""财务 Agent：ROE、营收增长、净利润、毛利率。

所有指标来自 Futu get_financials_statements 的真实财报字段；
接口未返回或字段无法核验时，明确标记"数据不足"，不生成虚构数字。
"""

import re

from .fundamental_base import (
    find_item, pick_metric, compute_yoy, score_by_thresholds, score_label,
    fmt_pct, fmt_num, to_number,
)

# 字段别名：覆盖中英文常见口径（含美股英文财报），"归母净利润"优先于"净利润"以避免口径混淆。
# 注意 normalise_name 会去除空格/括号/百分号等并转小写，因此英文别名用无空格的紧凑形式。
ALIASES = {
    "roe": [
        "净资产收益率", "roe", "returnon Equity", "returnonequity", "加权净资产收益率",
        "净资产收益率roe", "returnonaverageequity", "returnoncommonequity",
        "returnoncommonstockholdersequity",
    ],
    "revenue": [
        "营业总收入", "营业收入", "totalrevenue", "revenue", "operatingrevenue",
        "主营收入", "revenues", "salesrevenuenet", "netsales", "totaloperatingrevenue",
    ],
    "net_profit": [
        "归属于母公司股东的净利润", "归属于母公司所有者的净利润", "归母净利润",
        "netprofitattributabletoparent", "netprofit", "净利润",
        "netincome", "netincomeloss", "netincometocommonshareholders",
        "netincomeavailabletocommonstockholders",
    ],
    "gross_margin": [
        "销售毛利率", "毛利率", "grossprofitmargin", "grossmargin",
        "grossmarginonrevenue", "grossprofitmarginoftotalrevenue",
    ],
}


class FinancialAgent:
    """解析财报中的 ROE / 营收 / 净利润 / 毛利率，并输出规则化评分。"""

    @staticmethod
    def _period(report):
        return str(report.get("date_time_str") or report.get("period_text") or "未知报告期")

    @staticmethod
    def _ordered(rows):
        """把 Futu 的倒序报告列表整理为按报告期升序。

        有完整日期时按日期排序；否则依赖接口「最新在前」的约定反转。
        """
        if rows and all(re.match(r"^\d{4}[-/]\d{1,2}", r["period"]) for r in rows):
            return sorted(rows, key=lambda r: r["period"])
        return list(reversed(rows))

    @classmethod
    def _series(cls, reports, aliases):
        """按报告期抽取多期真实数值，不补 0、不插值。"""
        rows = []
        for report in reports or []:
            item = find_item(report, aliases)
            if item is None:
                continue
            value = to_number(item.get("data"))
            if value is None:
                continue
            rows.append({"period": cls._period(report), "value": value,
                         "field": str(item.get("display_name") or "未知字段")})
        # 同一报告期只保留最后一次出现
        deduped = {}
        for row in rows:
            deduped[row["period"]] = row
        return cls._ordered(list(deduped.values()))

    @staticmethod
    def _growth(series):
        """判断「是否持续增长」：至少三期且最近两期连续正增长才算成立。"""
        values = [r["value"] for r in series]
        changes = []
        for i in range(1, len(values)):
            changes.append(None if values[i - 1] == 0 else (values[i] / values[i - 1] - 1) * 100)
        valid = [c for c in changes if c is not None]
        streak = 0
        for c in reversed(valid):
            if c > 0:
                streak += 1
            else:
                break
        if len(values) < 3 or len(valid) < 2:
            status, sustained = "数据不足", None
            detail = f"仅取得 {len(values)} 期可核验数据，无法判断增长是否持续"
        elif streak >= 2:
            status, sustained = "是", True
            detail = f"近 {streak} 个相邻报告期连续增长（最近 {valid[-1]:+.2f}%）"
        elif streak == 1:
            status, sustained = "部分", False
            detail = "仅最近一期增长，更早一期未增长，尚不构成持续增长"
        else:
            status, sustained = "否", False
            detail = f"最近一期变动 {valid[-1]:+.2f}%，增长中断"
        return {
            "status": status, "sustained": sustained, "detail": detail,
            "periods": [r["period"] for r in series],
            "values": values, "period_changes_pct": changes,
            "consecutive_growth": streak, "latest_change_pct": valid[-1] if valid else None,
            "source_field": series[-1]["field"] if series else None,
            "as_of": series[-1]["period"] if series else None,
        }

    @staticmethod
    def _delta(series, threshold=0.5):
        """比率型指标（毛利率 / ROE）的环比改善，阈值 0.5 个百分点。"""
        if len(series) < 2:
            return {"status": "数据不足", "direction": "数据不足", "delta_pp": None,
                    "detail": "至少需要两期可核验数据才能判断改善或恶化",
                    "as_of": series[-1]["period"] if series else None}
        delta = series[-1]["value"] - series[-2]["value"]
        if delta > threshold:
            status, direction = "是", "改善"
        elif delta < -threshold:
            status, direction = "否", "恶化"
        else:
            status, direction = "部分", "持平"
        return {
            "status": status, "direction": direction, "delta_pp": round(delta, 2),
            "detail": f"{series[-2]['period']} {series[-2]['value']:.2f}% → "
                      f"{series[-1]['period']} {series[-1]['value']:.2f}%（{delta:+.2f} 个百分点）",
            "as_of": series[-1]["period"],
            "values": [r["value"] for r in series],
            "periods": [r["period"] for r in series],
        }

    @classmethod
    def _trend_analysis(cls, key_reports, income_reports):
        """基于 Futu 多期年报构造趋势证据；不引入任何外部推断。"""
        revenue = cls._series(income_reports or key_reports, ALIASES["revenue"])
        profit = cls._series(income_reports or key_reports, ALIASES["net_profit"])
        gross = cls._series(key_reports or income_reports, ALIASES["gross_margin"])
        roe = cls._series(key_reports or income_reports, ALIASES["roe"])
        return {
            "report_count": max(len(revenue), len(profit), len(gross), len(roe)),
            "revenue": cls._growth(revenue),
            "net_profit": cls._growth(profit),
            "gross_margin": cls._delta(gross),
            "roe": cls._delta(roe),
            "source": "Futu get_financials_statements（年报多期序列）",
            "note": "按报告期升序计算；未调整会计准则、合并范围与季度口径差异，跨期可比性需以财报原文核验。",
        }

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
                "growth_trend": self._trend_analysis([], []),
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

        trend = self._trend_analysis(key_reports, income_reports)
        trend_parts = [
            f"营收{trend['revenue']['status']}持续增长",
            f"净利润{trend['net_profit']['status']}持续增长",
            f"毛利率{trend['gross_margin']['direction']}",
            f"ROE {trend['roe']['direction']}",
        ]
        conclusion += " 多期趋势（" + "、".join(trend_parts) + "）：" + trend["note"]
        conclusion += " 毛利率阈值未做行业区分，跨行业比较需谨慎。"

        return {
            "agent": "财务 Agent", "status": "完成" if score is not None else "数据不足",
            "score": score, "dimensions": dimensions,
            "roe_pct": roe_value, "revenue_yoy_pct": revenue_yoy,
            "net_profit_yoy_pct": profit_yoy, "gross_margin_pct": gross_value,
            "growth_trend": trend,
            "conclusion": conclusion,
        }

    @staticmethod
    def _reports(bundle):
        if not isinstance(bundle, dict) or not bundle.get("available"):
            return []
        return bundle.get("reports") or []
