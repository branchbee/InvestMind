from datetime import datetime
from typing import TypedDict, Any
from langgraph.graph import StateGraph, END
from config import settings
from ashare_hunter.data.futu_provider import FutuProvider
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


def build_workflow():
    provider = FutuProvider(
        host=settings.futu_host,
        port=settings.futu_port,
        max_plate_refresh_per_run=settings.futu_max_plate_refresh_per_run,
        plate_cache_days=settings.futu_plate_cache_days,
        request_interval=settings.futu_request_interval,
    )
    market_a, sector_a = MarketAgent(), SectorAgent()
    stock_a, tech_a = StockAgent(), TechnicalAgent()
    research_a, report_a = ResearchAgent(), ReportAgent()

    def collect(state):
        spot = provider.get_spot()
        sectors_raw = provider.get_sectors(spot)
        return {
            "scan_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "spot": spot,
            "sectors_raw": sectors_raw,
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
        graph.add_node(name, fn)

    graph.set_entry_point("collect")
    graph.add_edge("collect", "market_agent")
    graph.add_edge("market_agent", "sector_agent")
    graph.add_edge("sector_agent", "stock_agent")
    graph.add_edge("stock_agent", "research_agent")
    graph.add_edge("research_agent", "report_agent")
    graph.add_edge("report_agent", END)
    return graph.compile()
