import functools
import logging
import time
from datetime import datetime
from typing import TypedDict, Any
from langgraph.graph import StateGraph, END
from config import settings

logger = logging.getLogger(__name__)


def _timed(name, fn):
    """LangGraph 节点包装：记录耗时、输出键与异常堆栈，不改变节点语义。"""
    @functools.wraps(fn)
    def wrapper(state):
        started = time.perf_counter()
        logger.debug("节点 %s 开始", name)
        try:
            out = fn(state)
        except Exception:
            logger.error("节点 %s 失败（耗时 %.3fs）", name,
                         time.perf_counter() - started, exc_info=True)
            raise
        keys = list(out.keys()) if isinstance(out, dict) else type(out).__name__
        logger.info("节点 %s 完成，耗时 %.3fs，输出键=%s",
                    name, time.perf_counter() - started, keys)
        return out
    return wrapper
from ashare_hunter.data.futu_provider import FutuProvider, filter_by_market_cap
from ashare_hunter.agents.market_agent import MarketAgent
from ashare_hunter.agents.sector_agent import SectorAgent
from ashare_hunter.agents.stock_agent import StockAgent
from ashare_hunter.agents.technical_agent import TechnicalAgent
from ashare_hunter.agents.research_agent import ResearchAgent
from ashare_hunter.agents.report_agent import ReportAgent


class HunterState(TypedDict, total=False):
    scan_time: str
    spot: Any
    sectors_raw: Any
    market_summary: dict
    top_sectors: Any
    top_stocks: Any
    explanation: str
    report_path: str
    market_cap_filter: dict


def build_workflow(min_market_cap_cny=None, missing_market_cap=None):
    """构建 A 股扫描工作流；两个参数支持 GUI 运行时覆盖 .env 的市值门槛。"""
    threshold = max(
        float(settings.a_min_market_cap_cny if min_market_cap_cny is None
              else (min_market_cap_cny or 0.0)), 0.0
    )
    policy = (
        settings.a_missing_market_cap if missing_market_cap is None
        else str(missing_market_cap).strip().lower()
    )
    logger.info("构建 A 股 LangGraph 扫描工作流：最低总市值=%.0f元（缺失策略=%s）",
                threshold, policy)
    provider = FutuProvider(
        host=settings.futu_host,
        port=settings.futu_port,
        max_plate_refresh_per_run=settings.futu_max_plate_refresh_per_run,
        plate_cache_days=settings.futu_plate_cache_days,
        request_interval=settings.futu_request_interval,
        us_exclude_otc=settings.us_exclude_otc,
        snapshot_batch_size=settings.us_snapshot_batch_size,
        a_min_market_cap=threshold,
        a_missing_market_cap=policy,
    )
    market_a, sector_a = MarketAgent(), SectorAgent()
    stock_a, tech_a = StockAgent(), TechnicalAgent()
    research_a, report_a = ResearchAgent(), ReportAgent()

    def collect(state):
        spot = provider.get_spot(min_market_cap=threshold)
        # 真实 FutuProvider 已在数据层过滤并回传统计；外部注入的离线
        # Provider 未内置该能力时，编排层兜底执行同一规则并同步快照缓存，
        # 避免被剔除标的从板块成分股缓存回流到候选表。
        cap_filter = dict(spot.attrs.get("market_cap_filter") or {})
        if not cap_filter:
            spot, cap_filter = filter_by_market_cap(spot, threshold, policy, market="SH")
            if hasattr(provider, "_spot_cache"):
                provider._spot_cache = spot.copy()
                provider._spot_market = "SH"
            spot.attrs["market_cap_filter"] = cap_filter
        logger.info("阶段[快照] A股 %d 条（门槛 %.0f 元，剔除 %d 只）",
                    len(spot), cap_filter.get("threshold", 0),
                    cap_filter.get("before", len(spot)) - cap_filter.get("after", len(spot)))
        if cap_filter.get("enabled"):
            logger.info("A 股市值过滤统计：%s", cap_filter)
            if not cap_filter.get("has_field"):
                logger.warning("A 股快照未提供总市值字段，市值门槛未实际生效")
        sectors_raw = provider.get_sectors(spot)
        return {
            "scan_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "spot": spot,
            "sectors_raw": sectors_raw,
            "market_cap_filter": cap_filter,
        }

    def market(state):
        return {"market_summary": market_a.run(state["spot"])}

    def sectors(state):
        return {"top_sectors": sector_a.run(state["sectors_raw"], settings.top_sectors)}

    def stocks(state):
        result = stock_a.run(
            state["top_sectors"], provider, settings.top_stocks, settings.min_amount_yuan
        )
        return {"top_stocks": tech_a.enrich(result, provider)}

    def research(state):
        return {
            "explanation": research_a.explain(
                state["market_summary"], state["top_sectors"], state["top_stocks"]
            )
        }

    def report(state):
        try:
            report_path = report_a.save(
                state["market_summary"], state["top_sectors"], state["top_stocks"],
                state["explanation"], state["scan_time"]
            )
            return {"report_path": report_path}
        finally:
            provider.close()

    graph = StateGraph(HunterState)
    for name, fn in [
        ("collect", collect),
        ("market_agent", market),
        ("sector_agent", sectors),
        ("stock_agent", stocks),
        ("research_agent", research),
        ("report_agent", report),
    ]:
        graph.add_node(name, _timed(name, fn))

    graph.set_entry_point("collect")
    graph.add_edge("collect", "market_agent")
    graph.add_edge("market_agent", "sector_agent")
    graph.add_edge("sector_agent", "stock_agent")
    graph.add_edge("stock_agent", "research_agent")
    graph.add_edge("research_agent", "report_agent")
    graph.add_edge("report_agent", END)
    return graph.compile()
