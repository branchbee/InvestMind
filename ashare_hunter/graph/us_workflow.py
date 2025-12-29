"""美股市场板块扫描与深度研究编排器。

设计要点：
1. 与 A 股共用同一条扫描链路：美股全市场快照 → 板块聚合 → 板块排名 →
   板块内选股 → 技术面补充 → 研究解读。数据源不同，编排逻辑对称。
2. 板块数据来自 Futu get_plate_list(Market.US, ...)，代码形如 US.BKxxxx；
   缓存通过 market 字段与 A 股隔离。
3. 个股深度研究复用 TradingAgentsOrchestrator（四层流水线）。
4. 不依赖 langgraph，规避部分环境的动态导入限制；纯 Python 顺序编排。
"""

import logging
import time
from datetime import datetime

import pandas as pd

from config import settings
from ashare_hunter.utils.logging import log_df
from ashare_hunter.data.futu_provider import FutuProvider, filter_by_market_cap
from ashare_hunter.agents.market_agent import MarketAgent
from ashare_hunter.agents.sector_agent import SectorAgent
from ashare_hunter.agents.stock_agent import StockAgent
from ashare_hunter.agents.technical_agent import TechnicalAgent
from ashare_hunter.agents.research_agent import ResearchAgent
from ashare_hunter.agents.trading_agents_orchestrator import TradingAgentsOrchestrator

logger = logging.getLogger(__name__)


class USMarketOrchestrator:
    """美股市场板块扫描 + 个股四层深度研究的统一入口。"""

    def __init__(self, provider=None, min_market_cap_usd=None, missing_market_cap=None):
        """``min_market_cap_usd`` 支持 GUI 运行时覆盖 .env 中的默认门槛。"""
        self.min_market_cap_usd = max(
            float(settings.us_min_market_cap_usd if min_market_cap_usd is None
                  else (min_market_cap_usd or 0.0)), 0.0
        )
        self.missing_market_cap = (
            settings.us_missing_market_cap if missing_market_cap is None
            else str(missing_market_cap).strip().lower()
        )
        self.provider = provider or FutuProvider(
            host=settings.futu_host,
            port=settings.futu_port,
            max_plate_refresh_per_run=settings.futu_max_plate_refresh_per_run,
            plate_cache_days=settings.futu_plate_cache_days,
            request_interval=settings.futu_request_interval,
            us_exclude_otc=settings.us_exclude_otc,
            snapshot_batch_size=settings.us_snapshot_batch_size,
            us_min_market_cap=self.min_market_cap_usd,
            us_missing_market_cap=self.missing_market_cap,
        )
        # 外部注入的 Provider（含离线测试替身）同步门槛，保证口径一致
        if hasattr(self.provider, "us_min_market_cap"):
            self.provider.us_min_market_cap = self.min_market_cap_usd
        if hasattr(self.provider, "us_missing_market_cap"):
            self.provider.us_missing_market_cap = self.missing_market_cap
        self._market_agent = MarketAgent()
        self._sector_agent = SectorAgent()
        self._stock_agent = StockAgent()
        self._technical_agent = TechnicalAgent()
        self._research_agent = ResearchAgent()

    def scan(self, symbols=None, message=None):
        """扫描美股全市场板块，返回与 A 股扫描兼容的结果结构。

        symbols 参数仅为兼容旧调用保留，已不再使用（观察池功能已移除）。
        """
        def emit(text):
            if message:
                message(text)

        started = time.perf_counter()
        logger.info(
            "美股全市场扫描开始：板块上限=%d 个股上限=%d 最低成交额=%.0f "
            "最低总市值=%.0f美元（缺失策略=%s）",
            settings.us_top_sectors, settings.us_top_stocks, settings.us_min_amount_usd,
            self.min_market_cap_usd, self.missing_market_cap,
        )

        emit("正在获取美股全市场快照……")
        stage = time.perf_counter()
        spot = self.provider.get_us_spot()
        # 真实 FutuProvider 已在数据层按市值过滤并回传统计；外部注入的
        # Provider（离线测试替身等）未内置该能力时，编排层兜底执行同一规则，
        # 并同步其快照缓存，避免「候选表里出现刚被过滤掉的小市值股」。
        cap_filter = dict(spot.attrs.get("market_cap_filter") or {})
        if not cap_filter:
            spot, cap_filter = filter_by_market_cap(
                spot, self.min_market_cap_usd, self.missing_market_cap, market="US"
            )
            if hasattr(self.provider, "_spot_cache"):
                self.provider._spot_cache = spot.copy()
                self.provider._spot_market = "US"
            spot.attrs["market_cap_filter"] = cap_filter
        logger.info(
            "阶段[快照] 完成：%d 条（市值门槛 %.0f 美元，剔除 %d 只），耗时 %.3fs",
            len(spot), cap_filter.get("threshold_usd", 0),
            cap_filter.get("before", len(spot)) - cap_filter.get("after", len(spot)),
            time.perf_counter() - stage,
        )
        log_df(logger, "美股快照(编排层)", spot)
        # 数据层把「OTC 源头过滤 / 坏标的自动隔离」统计放在 attrs 中，
        # 不改变 DataFrame 列结构，也不改变 get_us_spot 的函数签名。
        otc_excluded = int(spot.attrs.get("otc_excluded", 0) or 0)
        skipped = list(spot.attrs.get("snapshot_skipped", []) or [])
        if otc_excluded:
            emit(f"已过滤 {otc_excluded} 个 OTC/无行情权限标的，避免整批快照被拒绝。")
        if skipped:
            emit(f"另有 {len(skipped)} 个标的无行情权限，已自动跳过，扫描继续：{skipped[0]}")
        logger.info("快照容错统计：OTC 过滤 %d 个，无权限跳过 %d 个", otc_excluded, len(skipped))

        cap_threshold = float(cap_filter.get("threshold_usd", 0) or 0)
        if cap_filter.get("enabled"):
            emit(
                f"市值门槛 {cap_threshold / 1e8:.0f} 亿美元：过滤前 "
                f"{cap_filter.get('before', 0)} 只，剔除低市值 "
                f"{cap_filter.get('below', 0)} 只、市值缺失 "
                f"{cap_filter.get('missing_excluded', 0)} 只，"
                f"保留 {cap_filter.get('after', 0)} 只进入后续分析。"
            )
            if not cap_filter.get("has_field"):
                emit("本次快照未提供总市值字段，市值门槛未实际生效；请确认 Futu 行情权限或升级 SDK。")
            elif cap_filter.get("missing") and cap_filter.get("missing_policy") == "keep":
                emit(
                    f"注意：{cap_filter['missing']} 只标的总市值缺失（ETF / 优先股等"
                    "非正股市值字段为 N/A），已按 keep 保留并标注，未做市值判断；"
                    "如需一并排除可设 US_MISSING_MARKET_CAP=exclude。"
                )
            if cap_filter.get("after") == 0:
                emit("市值过滤后没有剩余标的：请调低 US_MIN_MARKET_CAP_USD（设为 0 关闭市值过滤）。")
            logger.info("美股市值过滤统计：%s", cap_filter)

        emit("正在计算美股市场概览……")
        stage = time.perf_counter()
        market_summary = self._market_agent.run(spot) if not spot.empty else {
            "market_regime": "数据不足", "up_count": 0, "down_count": 0,
            "flat_count": 0, "advance_decline_ratio": 0.0,
            "avg_change_pct": 0.0, "total_amount_yuan": 0.0,
        }
        market_summary = dict(market_summary)
        market_summary["currency"] = "USD"
        market_summary["amount_note"] = "成交额为美元，沿用 total_amount_yuan 字段以兼容现有展示层"
        logger.info("阶段[市场概览] regime=%s 耗时 %.3fs",
                    market_summary.get("market_regime"), time.perf_counter() - stage)

        emit("正在聚合美股行业/概念板块强度（首次会建立板块成分股缓存）……")
        stage = time.perf_counter()
        top_sectors = pd.DataFrame()
        if not spot.empty:
            try:
                sectors_raw = self.provider.get_sectors(spot, market="US")
                logger.info("阶段[板块聚合] 原始板块 %d 个，耗时 %.3fs",
                            len(sectors_raw), time.perf_counter() - stage)
                top_sectors = self._sector_agent.run(sectors_raw, settings.us_top_sectors)
            except RuntimeError as exc:
                logger.warning("美股板块聚合失败，降级为空板块：%s", exc)
                logger.debug("美股板块聚合异常堆栈", exc_info=True)
                emit(f"美股板块聚合暂不可用：{exc}")
        else:
            logger.warning("美股快照为空，跳过板块聚合")
        logger.info("阶段[板块排名] 强势板块 %d 个", len(top_sectors))
        log_df(logger, "美股强势板块", top_sectors)

        emit("正在从强势板块中筛选个股并补充技术面……")
        stage = time.perf_counter()
        if top_sectors.empty:
            top_stocks = pd.DataFrame()
            logger.info("无强势板块，跳过选股阶段")
        else:
            candidates = self._stock_agent.run(
                top_sectors, self.provider,
                top_n=settings.us_top_stocks,
                min_amount=settings.us_min_amount_usd,
                market="US",
            )
            logger.info("阶段[板块选股] 候选 %d 只，耗时 %.3fs",
                        len(candidates), time.perf_counter() - stage)
            top_stocks = self._technical_agent.enrich(candidates, self.provider)
            logger.info("阶段[技术面补充] 完成 %d 只，累计耗时 %.3fs",
                        len(top_stocks), time.perf_counter() - stage)
        top_stocks["market"] = "US"
        top_stocks["currency"] = "USD"
        log_df(logger, "美股候选个股", top_stocks)

        stage = time.perf_counter()
        explanation = self._research_agent.explain(market_summary, top_sectors, top_stocks)
        logger.info("阶段[研究解读] 完成，耗时 %.3fs", time.perf_counter() - stage)

        logger.info("美股全市场扫描完成：板块 %d 个，候选 %d 只，总耗时 %.3fs",
                    len(top_sectors), len(top_stocks), time.perf_counter() - started)
        return {
            "scan_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "market": "US",
            "currency": "USD",
            "spot": spot,
            "market_cap_filter": cap_filter,
            "market_summary": market_summary,
            "top_sectors": top_sectors,
            "top_stocks": top_stocks,
            "explanation": explanation,
        }

    def analyze(self, stock, market=None, sectors=None, message=None, macro=None):
        """对单只美股执行 TradingAgents 四层研究（复用同一条流水线）。"""
        if message:
            message(f"正在对 {stock.get('name', '')} ({stock.get('code', '')}) 执行美股四层研究……")
        stock = dict(stock)
        stock.setdefault("market", "US")
        stock.setdefault("currency", "USD")
        started = time.perf_counter()
        logger.info("美股四层研究开始：%s (%s)", stock.get("name", ""), stock.get("code", ""))
        result = TradingAgentsOrchestrator(self.provider).analyze(
            stock, market or {}, sectors, macro=macro
        )
        final = (result.get("trading_agents") or {}).get("final_recommendation") or {}
        logger.info("美股四层研究完成：动作=%s 仓位=%s 风险=%s 审核=%s 耗时 %.3fs",
                    final.get("action"), final.get("position"), final.get("risk_level"),
                    final.get("review"), time.perf_counter() - started)
        return result
