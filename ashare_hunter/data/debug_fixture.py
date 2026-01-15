"""CLI 离线调试用的确定性数据替身。

用途：``python main.py analyze --code XXX --offline`` 时，不连接 Futu OpenD
也能完整跑通 TradingAgents 四层流水线，方便改 Agent 规则后做回归与断点调试。

设计原则与生产代码一致：
- 只提供 FutuProvider 同名接口的返回结构，数值为合成样本，绝不冒充真实行情；
- ``sparse`` 场景把所有数据源置为 ``available=False``，用于验证「数据不足」时
  不编造证据、多空辩论不强行裁决的降级路径；
- ``bull`` / ``bear`` 分别构造偏多、偏空的证据组合，用于验证裁决方向。

注意：本模块只服务 CLI 调试，生产扫描与 GUI 不会加载它。
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd

SCENARIOS = ("bull", "bear", "sparse")


def fixture_stock(code, name, market="A", scenario="bull"):
    """构造与扫描候选表同构的单股 dict。"""
    us = str(market).upper() == "US"
    bear = scenario == "bear"
    return {
        "code": str(code),
        "name": str(name or code),
        "market": "US" if us else "A",
        "currency": "USD" if us else "CNY",
        "change_pct": -3.2 if bear else 3.1,
        "amount": 8.6e9,
        "turnover": 2.4,
        "volume_ratio": 1.6,
        "last_price": 88.0 if bear else 128.0,
        "market_cap": 3.2e11 if bear else 1.8e12,
        "sector_names": "人工智能,半导体",
        "score": 32.0 if bear else 88.0,
        "_fixture": True,
    }


def fixture_market(market="A", scenario="bull"):
    """构造市场概览上下文（辩论层会消费 market）。"""
    bear = scenario == "bear"
    return {
        "market_regime": "偏弱" if bear else "偏强",
        "advance_decline_ratio": 0.45 if bear else 1.85,
        "up_count": 1100 if bear else 3200,
        "down_count": 3100 if bear else 1200,
        "flat_count": 180,
        "avg_change_pct": -1.1 if bear else 1.3,
        "total_amount_yuan": 9.2e11,
        "fixture": True,
    }


def fixture_sectors(market="A", scenario="bull"):
    """构造强势板块上下文；含 AI / 半导体以触发宏观层的行情代理证据。"""
    bear = scenario == "bear"
    sign = -1 if bear else 1
    return pd.DataFrame([
        {
            "sector_name": "人工智能", "board_type": "concept",
            "score": 35.0 if bear else 88.0, "change_pct": sign * 3.4,
            "up_count": 8 if bear else 48, "down_count": 48 if bear else 8,
            "amount": 7.4e10, "breadth": 0.2 if bear else 0.86,
        },
        {
            "sector_name": "半导体", "board_type": "industry",
            "score": 40.0 if bear else 84.0, "change_pct": sign * 2.6,
            "up_count": 12 if bear else 42, "down_count": 42 if bear else 12,
            "amount": 6.1e10, "breadth": 0.25 if bear else 0.78,
        },
    ])


def fixture_macro(scenario="bull"):
    """供 --macro-json 参考的示例（默认不注入，避免把示例当成事实）。"""
    bull = scenario != "bear"
    return {
        "rate": {
            "judgement": "有利" if bull else "不利",
            "value": "示例：10Y 国债收益率 3.8%",
            "source": "调试手工录入（示例，非真实数据）",
            "as_of": "2026-11-19",
            "note": "仅用于验证宏观证据链路",
        },
        "industry": {
            "judgement": "提升" if bull else "回落",
            "value": "示例：AI 资本开支同比 +30%",
            "source": "调试手工录入（示例，非真实数据）",
            "as_of": "2026-11-19",
        },
    }


class DebugFixtureProvider:
    """只实现 StockAnalysisOrchestrator 会调用的数据接口。"""

    def __init__(self, code="SH.600519", name="调试样本", market="A", scenario="bull"):
        if scenario not in SCENARIOS:
            raise ValueError(f"未知 fixture 场景：{scenario}，可选 {', '.join(SCENARIOS)}")
        self.code = str(code)
        self.name = str(name or code)
        self.market = "US" if str(market).upper() == "US" else "A"
        self.scenario = scenario
        self.calls = []

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    @property
    def _sparse(self):
        return self.scenario == "sparse"

    @property
    def _bear(self):
        return self.scenario == "bear"

    def _unavailable(self, label):
        return {"available": False, "reason": f"离线 {self.scenario} 场景不提供{label}（调试用）"}

    def close(self):
        """与 FutuProvider.close 对齐，便于 CLI 用 try/finally 统一释放。"""

    # ------------------------------------------------------------------
    # 行情 / 财务 / 估值 / 新闻 / 资金接口
    # ------------------------------------------------------------------
    def get_daily_history(self, code, days=80):
        self.calls.append("get_daily_history")
        columns = ["date", "close", "volume", "pct_change"]
        if self._sparse:
            return pd.DataFrame(columns=columns)
        start = date(2026, 6, 1)
        dates, close, volume = [], [], []
        day = start
        for i in range(90):
            while day.weekday() >= 5:
                day += timedelta(days=1)
            dates.append(day.isoformat())
            day += timedelta(days=1)
            trend = -0.55 if self._bear else 0.5
            base = (160.0 if self._bear else 100.0) + i * trend + 1.1 * math.sin(i / 4.0)
            # 末段加速，制造均线突破与量能放大
            if i >= 85:
                base += (i - 84) * (-2.6 if self._bear else 2.8)
            close.append(round(base, 2))
            volume.append(int(1.0e6 * (1 + i / 90.0) * (1.9 if i >= 85 else 1.0)))
        pct = [None] + [
            round((close[i] / close[i - 1] - 1) * 100, 3) for i in range(1, len(close))
        ]
        return pd.DataFrame({
            "date": dates, "close": close, "volume": volume, "pct_change": pct,
        })

    def get_financials(self, code, statement_type=4, financial_type="ANNUAL", num=5):
        self.calls.append("get_financials")
        if self._sparse:
            return self._unavailable("财报")
        periods = ("2025-12-31", "2024-12-31", "2023-12-31")  # 最新在前
        # bull：营收/净利连续增长、毛利率与 ROE 逐期改善；bear 全部反向
        revenue = (1.50e10, 1.22e10, 1.00e10) if not self._bear else (0.82e10, 1.00e10, 1.20e10)
        profit = (2.60e9, 1.90e9, 1.50e9) if not self._bear else (0.90e9, 1.50e9, 2.10e9)
        margin = (41.0, 37.0, 34.0) if not self._bear else (28.0, 34.0, 39.0)
        roe = (19.5, 16.0, 13.5) if not self._bear else (7.5, 13.0, 18.0)
        reports = []
        for idx, period in enumerate(periods):
            items = [
                {"display_name": "营业总收入", "data": revenue[idx]},
                {"display_name": "归属于母公司股东的净利润", "data": profit[idx]},
                {"display_name": "销售毛利率", "data": margin[idx]},
                {"display_name": "净资产收益率", "data": roe[idx]},
            ]
            reports.append({
                "date_time_str": period, "period_text": period[:4] + "年报",
                "financial_type": financial_type, "item_list": items,
            })
        return {
            "available": True, "statement_type": statement_type,
            "financial_type": financial_type, "structure_list": [],
            "reports": reports, "next_key": None,
        }

    def get_single_snapshot(self, code):
        self.calls.append("get_single_snapshot")
        if self._sparse:
            return self._unavailable("快照")
        return {
            "available": True, "code": code,
            "price": 88.0 if self._bear else 128.0,
            "pe_ratio": 62.0 if self._bear else 27.0,
            "pe_ttm_ratio": 58.0 if self._bear else 25.0,
            "pb_ratio": 8.6 if self._bear else 3.8,
            "ps_ratio": 7.0 if self._bear else 3.2,
            "total_market_val": 3.2e11 if self._bear else 1.8e12,
            "update_time": "2026-11-18 16:00:00",
        }

    def get_valuation_detail(self, code, valuation_type="PE", interval_type="YEAR_5"):
        self.calls.append("get_valuation_detail")
        if self._sparse:
            return self._unavailable("估值详情")
        pct = 88.0 if self._bear else 28.0
        avg = 32.0 if self._bear else 34.0
        return {
            "available": True, "valuation_type": valuation_type,
            "interval_type": interval_type,
            "detail": {"trend": {
                "valuation_percentile": pct, "average_value": avg,
                "historical_items": [{"value": avg * (0.8 + i * 0.004)} for i in range(60)],
            }},
        }

    def get_news(self, keyword, max_count=20, news_sub_type="ALL"):
        self.calls.append("get_news")
        if self._sparse:
            return self._unavailable("资讯")
        name = self.name
        positive = {
            "NOTICE": [f"{name}发布新一代AI芯片并量产", f"{name}签订重大合同，订单金额创新高",
                       f"{name}2026年度业绩预增公告"],
            "NEWS": [f"{name}受益行业景气度提升，需求增长", f"{name}新品获批，技术突破"],
            "RATING": [f"{name}机构上调评级至买入", f"{name}上调目标价"],
        }
        negative = {
            "NOTICE": [f"{name}2026年度业绩预减公告", f"{name}收到监管问询函"],
            "NEWS": [f"{name}股东减持，行业需求下滑", f"{name}下调评级且下调目标价"],
            "RATING": [f"{name}机构下调评级至卖出"],
        }
        titles = (negative if self._bear else positive).get(news_sub_type, [])
        rows = [{
            "title": title,
            "source": "离线调试样本",
            "publish_time": "2026-11-18 09:30:00",
            "url": "",
            "news_sub_type": news_sub_type,
            "related_securities": [self.code],
        } for title in titles]
        return {"available": bool(rows), "keyword": keyword,
                "sub_type": news_sub_type, "rows": rows}

    def get_capital_flow(self, code, period_type="DAY", days=10):
        self.calls.append("get_capital_flow")
        if self._sparse:
            return self._unavailable("资金流")
        sign = -1 if self._bear else 1
        rows = [{
            "main_in_flow": sign * (6.0e7 + i * 8.0e6),
            "super_in_flow": sign * (3.0e7 + i * 4.0e6),
            "big_in_flow": sign * (3.0e7 + i * 4.0e6),
            "in_flow": sign * (1.1e8 + i * 1.0e7),
            "capital_flow_item_time": f"2026-11-{8 + i:02d}",
        } for i in range(6)]
        return {"available": True, "period_type": period_type,
                "rows": rows[-days:], "raw_count": len(rows)}

    def get_capital_distribution(self, code):
        self.calls.append("get_capital_distribution")
        if self._sparse:
            return self._unavailable("资金分布")
        sign = -1 if self._bear else 1
        return {"available": True, "distribution": {
            "capital_in_super": 6.0e7, "capital_in_big": 3.0e7,
            "capital_out_super": 2.0e7 if not self._bear else 6.0e7,
            "capital_out_big": 1.0e7 if not self._bear else 3.0e7,
            "update_time": "2026-11-18 16:00:00",
            "net_in_flow": sign * 8.0e7,
        }}

    def get_institutional_holdings(self, code, num=10):
        self.calls.append("get_institutional_holdings")
        if self._sparse:
            return self._unavailable("机构持股")
        return {"available": True, "rows": [
            {"holder_name": "调试机构A", "holding_ratio": 12.5 if not self._bear else 4.2},
            {"holder_name": "调试机构B", "holding_ratio": 8.3 if not self._bear else 2.1},
        ]}

    def get_analyst_consensus(self, code):
        self.calls.append("get_analyst_consensus")
        if self._sparse:
            return self._unavailable("分析师评级")
        return {"available": True, "rows": [
            {"rating": "买入" if not self._bear else "卖出", "target_price": 150.0 if not self._bear else 70.0},
        ]}
