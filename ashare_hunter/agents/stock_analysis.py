import json
import math
import pandas as pd
import numpy as np
import requests

from config import settings
from ashare_hunter.data.futu_provider import FutuProvider


class TrendAgent:
    """基于日线收盘价识别短中期趋势。"""

    def run(self, history):
        if history is None or history.empty or "close" not in history:
            return {"agent": "趋势 Agent", "status": "数据不足", "conclusion": "暂无足够日线数据判断趋势"}
        close = pd.to_numeric(history["close"], errors="coerce").dropna()
        if len(close) < 20:
            return {"agent": "趋势 Agent", "status": "数据不足", "conclusion": f"仅有 {len(close)} 根有效日线，至少需要 20 根"}
        last = float(close.iloc[-1])
        ma5, ma10, ma20 = (float(close.tail(n).mean()) for n in (5, 10, 20))
        if last > ma5 > ma10 > ma20:
            trend = "多头排列"
            conclusion = "收盘价位于 MA5、MA10、MA20 之上，短中期趋势偏强"
        elif last < ma5 < ma10 < ma20:
            trend = "空头排列"
            conclusion = "收盘价低于 MA5、MA10、MA20，短中期趋势偏弱"
        elif last > ma20:
            trend = "震荡偏强"
            conclusion = "价格仍在 MA20 上方，但均线尚未形成完整多头排列"
        else:
            trend = "震荡偏弱"
            conclusion = "价格位于 MA20 下方，趋势强度不足"
        return {
            "agent": "趋势 Agent", "status": "完成", "trend": trend,
            "last_close": last, "ma5": ma5, "ma10": ma10, "ma20": ma20,
            "conclusion": conclusion
        }


class MomentumAgent:
    """计算多周期收益、RSI 与 MACD，不预测未来收益。"""

    def run(self, history):
        if history is None or history.empty or "close" not in history:
            return {"agent": "动量 Agent", "status": "数据不足", "conclusion": "暂无历史行情"}
        close = pd.to_numeric(history["close"], errors="coerce").dropna()
        if len(close) < 15:
            return {"agent": "动量 Agent", "status": "数据不足", "conclusion": "历史 K 线不足，无法稳定计算动量指标"}
        returns = {}
        for n in (5, 10, 20):
            returns[f"return_{n}d_pct"] = float((close.iloc[-1] / close.iloc[-n-1] - 1) * 100) if len(close) > n else None
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        rsi_value = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal = macd_line.ewm(span=9, adjust=False).mean()
        macd_hist = float((macd_line - signal).iloc[-1])
        if rsi_value is None:
            momentum = "指标不足"
        elif rsi_value >= 75:
            momentum = "短线偏热"
        elif rsi_value >= 55 and macd_hist > 0:
            momentum = "动量偏强"
        elif rsi_value <= 35:
            momentum = "动量偏弱"
        else:
            momentum = "中性"
        return {
            "agent": "动量 Agent", "status": "完成", **returns,
            "rsi14": rsi_value, "macd_hist": macd_hist,
            "momentum": momentum,
            "conclusion": f"动量状态：{momentum}；RSI(14)={rsi_value:.1f}" if rsi_value is not None else f"动量状态：{momentum}"
        }


class LiquidityAgent:
    """使用快照字段评估成交活跃度；不把成交额误当作资金流入。"""

    def run(self, stock):
        amount = pd.to_numeric(pd.Series([stock.get("amount")]), errors="coerce").iloc[0]
        turnover = pd.to_numeric(pd.Series([stock.get("turnover")]), errors="coerce").iloc[0]
        volume_ratio = pd.to_numeric(pd.Series([stock.get("volume_ratio")]), errors="coerce").iloc[0]
        parts = []
        if pd.notna(amount):
            parts.append(f"成交额 {amount / 1e8:.2f} 亿元")
        else:
            parts.append("成交额未知")
        if pd.notna(turnover):
            parts.append(f"换手率字段 {turnover:.2f}")
        if pd.notna(volume_ratio):
            parts.append(f"量比 {volume_ratio:.2f}")
        conclusion = "；".join(parts) + "。成交额和换手率只反映交易活跃度，不代表净流入。"
        return {
            "agent": "流动性 Agent", "status": "完成",
            "amount": float(amount) if pd.notna(amount) else None,
            "turnover": float(turnover) if pd.notna(turnover) else None,
            "volume_ratio": float(volume_ratio) if pd.notna(volume_ratio) else None,
            "conclusion": conclusion
        }


class RiskAgent:
    """根据可用价格和日线波动做风险提示，不替代风险模型。"""

    def run(self, stock, history):
        change = pd.to_numeric(pd.Series([stock.get("change_pct")]), errors="coerce").iloc[0]
        close = pd.to_numeric(history["close"], errors="coerce").dropna() if history is not None and "close" in history else pd.Series(dtype=float)
        risk_flags = []
        if pd.notna(change) and change >= 7:
            risk_flags.append("当日涨幅较大，追高回撤风险上升")
        if pd.notna(change) and change <= -5:
            risk_flags.append("当日跌幅较大，需关注趋势破位与流动性风险")
        if len(close) >= 21:
            daily_ret = close.pct_change().dropna().tail(20)
            vol = float(daily_ret.std() * math.sqrt(交易日年化) * 100) if len(daily_ret) >= 5 else None
            peak = close.tail(20).cummax()
            drawdown = float(((close.tail(20) / peak) - 1).min() * 100)
            if vol is not None and vol >= 4:
                risk_flags.append("近20日波动较高")
            if drawdown <= -10:
                risk_flags.append("近20日最大回撤超过 10%")
        else:
            vol, drawdown = None, None
            risk_flags.append("历史数据不足，无法评估波动与回撤")
        if not risk_flags:
            risk_flags.append("基于当前可用数据未触发简单风险阈值；不代表没有风险")
        return {
            "agent": "风险 Agent", "status": "完成",
            "annualized_volatility_pct": vol, "max_drawdown_20d_pct": drawdown,
            "risk_flags": risk_flags, "conclusion": "；".join(risk_flags)
        }


交易日年化 = 252


class SectorContextAgent:
    def run(self, stock, sectors):
        names = str(stock.get("sector_names") or stock.get("sector_name") or "")
        matched = []
        if sectors is not None and not sectors.empty and names:
            for _, sector in sectors.iterrows():
                sector_name = str(sector.get("sector_name", ""))
                if sector_name and sector_name in names:
                    matched.append({
                        "sector_name": sector_name,
                        "change_pct": float(sector.get("change_pct", 0)),
                        "score": float(sector.get("score", 0)),
                        "breadth": float(sector.get("breadth", 0))
                    })
        if matched:
            summary = "；".join(
                f"{x['sector_name']}：涨幅 {x['change_pct']:.2f}%，板块评分 {x['score']:.1f}"
                for x in matched
            )
            conclusion = "所属强势板块：" + summary
        else:
            conclusion = "当前扫描结果中未匹配到该股票所属的强势板块信息"
        return {"agent": "板块 Agent", "status": "完成", "matched_sectors": matched, "conclusion": conclusion}


from .financial_agent import FinancialAgent
from .valuation_agent import ValuationAgent
from .news_agent import NewsAgent
from .capital_agent import CapitalAgent
from .decision_agent import DecisionAgent


class StockAnalysisOrchestrator:
    def __init__(self, provider=None):
        self.provider = provider or FutuProvider(
            host=settings.futu_host, port=settings.futu_port,
            max_plate_refresh_per_run=settings.futu_max_plate_refresh_per_run,
            plate_cache_days=settings.futu_plate_cache_days,
            request_interval=settings.futu_request_interval
        )

    @staticmethod
    def _safe_fetch(label, fn, *args, **kwargs):
        """单个数据源失败不影响其他 Agent；失败时返回可追溯的不可用标记。"""
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            return {"available": False, "reason": f"{label} 调用失败：{exc}"}

    def analyze(self, stock, market=None, sectors=None):
        code = str(stock.get("code", ""))
        name = str(stock.get("name", ""))

        history = self._safe_fetch("历史K线", self.provider.get_daily_history, code, days=80)
        if not isinstance(history, pd.DataFrame):
            history = pd.DataFrame(columns=["date", "close"])

        # 基本面 / 估值 / 新闻 / 资金：逐项容错拉取
        key_ind = self._safe_fetch("关键财务指标", self.provider.get_financials,
                                   code, statement_type=4, financial_type="ANNUAL",
                                   num=settings.financial_report_count)
        income = self._safe_fetch("利润表", self.provider.get_financials,
                                  code, statement_type=1, financial_type="ANNUAL",
                                  num=settings.financial_report_count)
        snapshot = self._safe_fetch("行情快照", self.provider.get_single_snapshot, code)
        pe_detail = self._safe_fetch("PE估值", self.provider.get_valuation_detail,
                                     code, valuation_type="PE", interval_type=settings.valuation_interval)
        pb_detail = self._safe_fetch("PB估值", self.provider.get_valuation_detail,
                                     code, valuation_type="PB", interval_type=settings.valuation_interval)
        notices = self._safe_fetch("公告检索", self.provider.get_news,
                                   name, max_count=settings.news_max_count, news_sub_type="NOTICE")
        news = self._safe_fetch("资讯检索", self.provider.get_news,
                                name, max_count=settings.news_max_count, news_sub_type="NEWS")
        ratings = self._safe_fetch("评级检索", self.provider.get_news,
                                   name, max_count=settings.news_max_count, news_sub_type="RATING")
        capital_flow = self._safe_fetch("资金流", self.provider.get_capital_flow,
                                        code, period_type="DAY", days=settings.capital_flow_days)
        distribution = self._safe_fetch("资金分布", self.provider.get_capital_distribution, code)
        institutional = self._safe_fetch("机构持股", self.provider.get_institutional_holdings, code)
        consensus = self._safe_fetch("分析师评级", self.provider.get_analyst_consensus, code)

        fin_agent = FinancialAgent().run(key_ind, income)
        valuation_agent = ValuationAgent().run(
            snapshot=snapshot, pe_detail=pe_detail, pb_detail=pb_detail,
            profit_growth_pct=fin_agent.get("net_profit_yoy_pct"),
        )
        news_agent = NewsAgent().run(notices=notices, news=news, ratings=ratings, stock_name=name)
        capital_agent = CapitalAgent().run(
            capital_flow=capital_flow, distribution=distribution,
            institutional=institutional, analyst_consensus=consensus,
        )

        agents = [
            TrendAgent().run(history),
            MomentumAgent().run(history),
            LiquidityAgent().run(stock),
            RiskAgent().run(stock, history),
            SectorContextAgent().run(stock, sectors),
            fin_agent,
            valuation_agent,
            news_agent,
            capital_agent,
        ]
        decision_agent = DecisionAgent().run(
            agents, market=market, sectors=sectors, stock=stock,
            weights=settings.decision_weights(),
        )
        agents.append(decision_agent)

        result = {
            "stock": stock,
            "market": market or {},
            "agents": agents,
            "history_rows": int(len(history)),
            "history_latest_date": str(history["date"].iloc[-1]) if not history.empty and "date" in history else "未知",
            "data_sources": {
                "key_indicators": key_ind,
                "income_statement": income,
                "snapshot": snapshot,
                "pe_detail": pe_detail,
                "pb_detail": pb_detail,
                "capital_flow": capital_flow,
                "distribution": distribution,
                "institutional": institutional,
            },
        }
        result["overall"] = self._overall(agents)
        result["llm_summary"] = self._llm_summary(result)
        return result

    @staticmethod
    def _overall(agents):
        by_name = {a["agent"]: a for a in agents}
        decision = by_name.get("决策 Agent", {})
        if decision:
            return {
                "label": decision.get("rating", "待判断"),
                "explanation": decision.get("conclusion", ""),
                "score": decision.get("composite_score"),
                "confidence": decision.get("confidence"),
                "coverage": decision.get("coverage"),
                "investment_logic": decision.get("investment_logic", []),
                "risk_factors": decision.get("risk_factors", []),
            }
        trend = by_name.get("趋势 Agent", {}).get("trend", "")
        momentum = by_name.get("动量 Agent", {}).get("momentum", "")
        risk_flags = by_name.get("风险 Agent", {}).get("risk_flags", [])
        if trend == "多头排列" and momentum == "动量偏强" and not any("回撤超过" in x or "波动较高" in x for x in risk_flags):
            label = "技术面偏强"
        elif trend in ("空头排列", "震荡偏弱") or momentum == "动量偏弱":
            label = "技术面偏弱"
        else:
            label = "技术面中性 / 需观察"
        return {
            "label": label,
            "explanation": "综合趋势、动量、流动性、风险和板块环境形成研究结论；基本面数据未接入，因此不能形成完整投资判断。"
        }

    @staticmethod
    def _llm_summary(result):
        if not settings.llm_enabled or not settings.llm_api_key:
            return "LLM 总结未启用。上方各专项 Agent 的规则计算结果仍可独立查看。"
        safe = {
            "stock": result["stock"],
            "market": result["market"],
            "agents": result["agents"],
            "overall": result["overall"],
            "instruction": (
                "只解释提供的数据，不补充任何未提供的财务、公告、新闻、资金流或目标价。"
                "缺失项明确写未知；不得把规则评分表述为确定性买卖建议。"
            )
        }
        payload = {
            "model": settings.llm_model, "temperature": 0.1,
            "messages": [{"role": "user", "content":
                "你是审慎的A股研究分析师。请根据以下结构化多Agent结果，输出：1.核心结论；2.支持因素；3.主要风险；4.尚缺数据；5.后续观察点。不得给出确定性买卖指令，不得编造数据。\n" +
                json.dumps(safe, ensure_ascii=False, default=str)}]
        }
        try:
            response = requests.post(
                settings.llm_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + settings.llm_api_key},
                json=payload, timeout=60
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            return f"LLM 总结调用失败：{exc}。请以各专项 Agent 的结构化结果为准。"
