"""A-Share Hunter 命令行入口。

两种用途：

1. 市场扫描（默认）：``python main.py`` 与旧版完全一致，A 股全市场扫描；
   ``python main.py scan --market US`` 扫描美股全市场。
2. TradingAgents 四层研究调试：``python main.py analyze --code SH.600519``
   直接对单只股票跑「分析师 → 多空辩论 → Trader → 风险管理」，无需先做
   全市场扫描；``--offline`` 使用确定性样本数据，不连接 Futu OpenD。

调试辅助：分层摘要、``--detail`` 完整证据表、``--macro-json`` 外部宏观录入、
``--json-out`` 导出完整结构化结果、``--log-level DEBUG`` 打开全链路日志。
完整用法见 ``python main.py --help`` 与 README 第十一章。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ashare_hunter.utils.logging import (
    setup_logging,
    shutdown_logging,
    current_log_file,
)
from config import settings

logger = logging.getLogger(__name__)

MARKET_A = "A"
MARKET_US = "US"
FIXTURE_SCENARIOS = ("bull", "bear", "sparse")

EXAMPLES = """常用示例:
  python main.py                                    # 旧行为：A 股全市场扫描
  python main.py scan --market US                   # 美股全市场板块扫描
  python main.py scan --analyze SH.600519           # 扫描后带真实上下文跑四层研究
  python main.py analyze --code SH.600519           # 直连 OpenD 调试单股四层
  python main.py analyze --code US.AAPL --market US --offline   # 离线复现，不连 OpenD
  python main.py analyze --code SH.600519 --offline --fixture bear --detail
  python main.py analyze --code SH.600519 --offline --macro-json @output/macro.json
  python main.py analyze --code SH.600519 --json-out output/result.json --log-level DEBUG

macro-json 示例（外部录入必须带 source 与 as_of，默认不注入）:
  {"rate": {"judgement": "有利", "source": "央行公告", "as_of": "2026-11-19"},
   "industry": {"judgement": "提升", "source": "行业协会月报", "as_of": "2026-11-19"}}
"""


# ----------------------------------------------------------------------
# 参数解析
# ----------------------------------------------------------------------
def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-level", default=None, help="覆盖 LOG_LEVEL，例如 DEBUG / INFO（默认读 .env）")
    common.add_argument("--log-file", default=None, help="覆盖滚动日志文件路径（默认 output/logs/investmind.log）")
    common.add_argument("--json-out", default=None, help="导出完整结果为 JSON；传目录时自动命名")
    common.add_argument("--no-save", action="store_true", help="不自动保存四层研究结果到 output/trading_agents/")
    common.add_argument("--no-llm", action="store_true", help="本次运行临时关闭 LLM 总结（离线模式自动关闭）")
    common.add_argument("--detail", action="store_true", help="打印四层证据检查表、多空论点、决策依据与风控检查项")
    common.add_argument("--macro-json", default=None, help="宏观录入：JSON 字符串 / 文件路径 / @文件路径")

    parser = argparse.ArgumentParser(
        prog="main.py",
        description="A-Share Hunter：市场扫描与 TradingAgents 四层研究调试入口",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", parents=[common], help="全市场扫描（默认子命令）")
    scan.add_argument(
        "--market", choices=(MARKET_A, MARKET_US), default=MARKET_A, help="A=A股（沪深），US=美股（全市场）"
    )
    scan.add_argument(
        "--min-cap", type=float, default=None, help="最低总市值，单位亿元(A)/亿美元(US)；0 关闭，缺省读 .env"
    )
    scan.add_argument(
        "--analyze", metavar="CODE", default=None, help="扫描完成后，对指定代码执行 TradingAgents 四层研究"
    )

    analyze = sub.add_parser("analyze", parents=[common], help="对单只股票执行 TradingAgents 四层研究")
    analyze.add_argument("--code", required=True, help="股票代码，如 SH.600519 / US.AAPL")
    analyze.add_argument("--name", default="", help="股票名称（影响资讯检索关键词）")
    analyze.add_argument(
        "--market", choices=(MARKET_A, MARKET_US), default=MARKET_A,
        help="A=A股（默认），US=美股；美股代码请显式指定 --market US",
    )
    analyze.add_argument("--offline", action="store_true", help="使用确定性调试样本，不连接 Futu OpenD，不发 LLM 请求")
    analyze.add_argument(
        "--fixture",
        choices=FIXTURE_SCENARIOS,
        default="bull",
        help="离线样本场景：bull 偏多 / bear 偏空 / sparse 全数据不足",
    )
    analyze.add_argument("--market-json", default=None, help="手工注入市场概览上下文（JSON 字符串或文件路径）")
    analyze.add_argument("--sectors-json", default=None, help="手工注入强势板块上下文（JSON 数组或文件路径）")
    analyze.add_argument(
        "--sector",
        action="append",
        default=[],
        metavar="NAME",
        help="追加一条板块上下文，可重复；与 --sectors-json 合并",
    )
    return parser


def parse_args(argv=None):
    """无子命令时按 scan 处理，保证 ``python main.py`` 与旧版完全兼容。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        pass
    elif not argv or argv[0].startswith("-") or argv[0] not in ("scan", "analyze"):
        argv = ["scan"] + argv
    return build_parser().parse_args(argv)


# ----------------------------------------------------------------------
# 上下文 / 参数工具
# ----------------------------------------------------------------------
def _load_json_arg(value, label):
    """支持内联 JSON、文件路径、``@文件路径`` 三种写法。"""
    if value is None:
        return None
    text = str(value).strip()
    path = Path(text[1:] if text.startswith("@") else text)
    if text.startswith("@") or (len(text) < 260 and path.is_file()):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ValueError(f"无法读取 {label} 文件 {path}：{exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} 不是合法 JSON：{exc}") from exc


def _sectors_frame(args):
    """把 --sectors-json / --sector 统一成 DataFrame（SectorContextAgent 需要 .empty）。"""
    import pandas as pd

    rows = []
    loaded = _load_json_arg(args.sectors_json, "--sectors-json")
    if isinstance(loaded, dict):
        loaded = loaded.get("sectors") or loaded.get("rows") or [loaded]
    if isinstance(loaded, list):
        rows = [r for r in loaded if isinstance(r, dict)]
    elif loaded is not None:
        raise ValueError("--sectors-json 需要是对象数组或含 sectors 字段的对象")
    for name in getattr(args, "sector", []) or []:
        rows.append(
            {
                "sector_name": name,
                "board_type": "concept",
                "score": 80.0,
                "change_pct": 2.5,
                "up_count": 30,
                "down_count": 10,
                "amount": 5.0e10,
                "breadth": 0.75,
            }
        )
    return pd.DataFrame(rows) if rows else None


def _macro(args):
    macro = _load_json_arg(args.macro_json, "--macro-json")
    if macro is not None and not isinstance(macro, dict):
        raise ValueError('--macro-json 需要是对象，例如 {"rate": {...}}')
    return macro


def _code_matches(left, right):
    a, b = str(left or "").strip().upper(), str(right or "").strip().upper()
    if not a or not b:
        return False
    if a == b or a.endswith("." + b) or b.endswith("." + a):
        return True
    # 允许只输入数字代码（600519）匹配 SH.600519
    return (a.isdigit() and b.endswith(a)) or (b.isdigit() and a.endswith(b))


def _find_stock(frame, code):
    if frame is None or not hasattr(frame, "to_dict") or getattr(frame, "empty", True):
        return None
    for row in frame.to_dict("records"):
        if _code_matches(row.get("code"), code):
            return row
    return None


def _make_provider(args, min_cap=None):
    """真实 Provider 与 GUI 使用同一组参数；离线时返回确定性替身。"""
    if getattr(args, "offline", False):
        from ashare_hunter.data.debug_fixture import DebugFixtureProvider

        return DebugFixtureProvider(
            args.code,
            args.name or args.code,
            args.market,
            args.fixture,
        )
    from ashare_hunter.data.futu_provider import FutuProvider

    cap = None if min_cap is None else max(float(min_cap), 0.0)
    return FutuProvider(
        host=settings.futu_host,
        port=settings.futu_port,
        max_plate_refresh_per_run=settings.futu_max_plate_refresh_per_run,
        plate_cache_days=settings.futu_plate_cache_days,
        request_interval=settings.futu_request_interval,
        us_exclude_otc=settings.us_exclude_otc,
        snapshot_batch_size=settings.us_snapshot_batch_size,
        us_min_market_cap=(settings.us_min_market_cap_usd if cap is None else cap),
        us_missing_market_cap=settings.us_missing_market_cap,
        a_min_market_cap=(settings.a_min_market_cap_cny if cap is None else cap),
        a_missing_market_cap=settings.a_missing_market_cap,
    )


def _default_stock(args):
    if getattr(args, "offline", False):
        from ashare_hunter.data.debug_fixture import fixture_stock

        return fixture_stock(args.code, args.name or args.code, args.market, args.fixture)
    us = args.market == MARKET_US
    return {
        "code": args.code,
        "name": args.name or args.code,
        "market": "US" if us else "A",
        "currency": "USD" if us else "CNY",
    }


@contextmanager
def _llm_guard(args):
    """离线调试或 --no-llm 时临时关闭 LLM，结束后恢复用户配置。"""
    disable = bool(args.no_llm or getattr(args, "offline", False))
    previous = settings.llm_enabled
    if disable:
        settings.llm_enabled = False
    try:
        yield
    finally:
        settings.llm_enabled = previous


# ----------------------------------------------------------------------
# 结果输出
# ----------------------------------------------------------------------
def _json_default(value):
    if hasattr(value, "columns") and hasattr(value, "to_dict"):
        return value.to_dict("records")
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (set, tuple)):
        return list(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _write_json(payload, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=_json_default)
    return path


def _result_path(args, code, suffix="trading_agents"):
    """--json-out 优先；未指定时自动留档到 output/trading_agents/。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_code = re.sub(r"[^A-Za-z0-9._-]", "_", str(code))
    filename = f"{suffix}_{safe_code}_{stamp}.json"
    if args.json_out:
        path = Path(args.json_out)
        if path.is_dir() or str(path).endswith(("/", "\\")):
            path = path / filename
    else:
        if args.no_save:
            return None
        path = Path("output") / "trading_agents" / filename
    return path


def _print_scan(result, market):
    cap = result.get("market_cap_filter") or {}
    unit = "亿美元" if market == MARKET_US else "亿元"
    print(f"\n===== {'US-Stock' if market == MARKET_US else 'A-Share'} Hunter 扫描完成 =====")
    print("扫描时间:", result.get("scan_time"))
    print("市场环境:", result.get("market_summary", {}).get("market_regime"))
    print("强势板块:", len(result.get("top_sectors", [])))
    print("候选股票:", len(result.get("top_stocks", [])))
    print("报告:", result.get("report_path"))
    if cap.get("enabled"):
        print(
            f"市值门槛: {cap.get('threshold', 0) / 1e8:.1f} {unit}；"
            f"过滤前 {cap.get('before', 0)} 只 → 保留 {cap.get('after', 0)} 只"
            f"（低市值剔除 {cap.get('below', 0)} 只）"
        )
        if not cap.get("has_field"):
            print("注意: 快照未提供 total_market_val，市值门槛未实际生效。")
    print("调试日志:", current_log_file())


def _fmt_score(value):
    return f"{value:.1f}" if isinstance(value, (int, float)) else "—"


def _print_analysis(result, detail=False, json_path=None, mode=""):
    ta = result.get("trading_agents") or {}
    final = ta.get("final_recommendation") or {}
    debate = ta.get("research") or {}
    trader = ta.get("trading") or {}
    risk = ta.get("risk") or {}
    overall = result.get("overall") or {}
    stock = result.get("stock") or {}
    cov = debate.get("evidence_coverage") or {}

    print("\n" + "=" * 72)
    print(f"TradingAgents 四层研究：{stock.get('name', '')}（{stock.get('code', '')}）")
    print("=" * 72)
    if mode:
        print("运行模式:", mode)
    print(
        f"[最终建议] {final.get('action', '—')}（{final.get('action_code', '—')}）"
        f" ｜ 裁决 {final.get('verdict', '—')} ｜ 仓位 {final.get('position', '—')}"
    )
    print(
        f"            风险 {final.get('risk_level', '—')} ｜ 审核 {final.get('review', '—')}"
        f" ｜ 置信度 {trader.get('confidence', '—')}"
    )
    print(
        f"            综合评分 {_fmt_score(overall.get('score'))}/100"
        f" ｜ 维度覆盖 {float(overall.get('coverage') or 0):.0%}"
        f" ｜ 日线 {result.get('history_rows', 0)} 根（{result.get('history_latest_date', '未知')}）"
    )

    history_diag = (result.get("data_sources") or {}).get("history") or {}
    if history_diag and not history_diag.get("available", True):
        print(f"[日线数据] 不可用：{history_diag.get('reason')}")
        print(f"            返回 {history_diag.get('rows', 0)} 行 / 有效收盘 "
              f"{history_diag.get('valid_close_rows', 0)} 根；趋势、动量 Agent 与技术面证据将标记数据不足")
        print("            排查：Futu OpenD 连接、历史 K 线行情权限、标的退市/停牌或次新股、接口限频")
    elif detail and history_diag:
        print(f"[日线数据] 可用 · {history_diag.get('rows', 0)} 行 / "
              f"{history_diag.get('valid_close_rows', 0)} 根有效收盘 · "
              f"最新 {history_diag.get('latest_date') or '未知'}")

    print("\n[第一层 · 分析师团队]")
    for agent in result.get("agents", []):
        score = agent.get("score")
        score_text = f" 评分 {_fmt_score(score)}" if score is not None else ""
        print(f"  - {agent.get('agent', '?')}：{agent.get('status', '—')}{score_text}")
        if detail:
            print(f"      {agent.get('conclusion', '')}")

    print("\n[第二层 · 多空研究辩论]")
    judge = debate.get("judge") or {}
    print(
        f"  裁决 {judge.get('verdict', '—')}（胜方 {judge.get('winner', '—')}）"
        f" ｜ 可判定证据 {cov.get('answered', 0)}/{cov.get('total', 0)}"
        f" ｜ 多头论点 {len((debate.get('bull_case') or {}).get('arguments') or [])} 条"
        f" ｜ 空头论点 {len((debate.get('bear_case') or {}).get('arguments') or [])} 条"
    )
    by_layer = cov.get("by_layer") or {}
    if by_layer:
        print(
            "  四层覆盖："
            + " | ".join(
                f"{layer} {stat.get('answered', 0)}/{stat.get('total', 0)}" for layer, stat in by_layer.items()
            )
        )
    gaps = debate.get("unresolved_questions") or []
    if gaps:
        print(f"  尚缺数据 {len(gaps)} 项：" + "；".join(str(x) for x in gaps[:4]) + ("……" if len(gaps) > 4 else ""))
    if detail:
        for side, label in (("bull_case", "多头论点"), ("bear_case", "空头论点")):
            arguments = (debate.get(side) or {}).get("arguments") or []
            print(f"  {label}（{len(arguments)} 条）：")
            for item in arguments[:10]:
                print(f"    - [{item.get('source')}·{item.get('strength')}] {item.get('text')}")
            if len(arguments) > 10:
                print(f"    ... 其余 {len(arguments) - 10} 条见导出的 JSON")
        print("  证据检查表：")
        for item in debate.get("evidence_matrix") or []:
            print(
                f"    [{item.get('layer')}｜{item.get('answer')}｜{item.get('direction')}] "
                f"{item.get('question')} — {item.get('detail')}（{item.get('source')}）"
            )

    print("\n[第三层 · Trader]")
    print(
        f"  {trader.get('action', '—')} ｜ 置信度 {trader.get('confidence', '—')}"
        f" ｜ 周期 {trader.get('time_horizon', '—')}"
    )
    invalidation = trader.get("invalidation") or "—"
    if isinstance(invalidation, (list, tuple)):
        invalidation = "；".join(str(x) for x in invalidation)
    print(f"  关注区间：{trader.get('entry_zone', '—')}")
    print(f"  失效条件：{invalidation}")
    if detail:
        print(f"  决策依据：{trader.get('reason', '')}")

    print("\n[第四层 · 风险管理]")
    position_size = risk.get("position_size") or {}
    print(
        f"  风险 {risk.get('risk_level', '—')}（{risk.get('risk_score', '—')}/100）"
        f" ｜ 仓位 {position_size.get('recommended', '—')}"
        f" ｜ 审核 {(risk.get('strategy_review') or {}).get('result', '—')}"
    )
    conditions = (risk.get("final_decision") or {}).get("conditions") or []
    if conditions:
        print("  前置条件：" + "；".join(str(x) for x in conditions))
    if detail:
        for check in risk.get("risk_checks") or []:
            print(f"    [{check.get('level')}] {check.get('name')}：{check.get('detail')}")

    print("\n[数据边界] 规则化研究信号，不构成投资建议；数据不足项不参与裁决，" "宏观结论仅来自显式外部录入。")
    if json_path:
        print("完整结果:", json_path)
    print("调试日志:", current_log_file())


# ----------------------------------------------------------------------
# 执行流程
# ----------------------------------------------------------------------
def analyze_stock(args, *, stock=None, market_summary=None, sectors=None, provider=None, mode=""):
    """跑一次四层研究并输出摘要；返回结果 dict，异常交由 main 统一处理。"""
    from ashare_hunter.agents.trading_agents_orchestrator import TradingAgentsOrchestrator

    stock = stock or _default_stock(args)
    if market_summary is None:
        market_summary = _load_json_arg(getattr(args, "market_json", None), "--market-json") or {}
        if not isinstance(market_summary, dict):
            raise ValueError("--market-json 需要是对象")
        if getattr(args, "offline", False) and not market_summary:
            from ashare_hunter.data.debug_fixture import fixture_market

            market_summary = fixture_market(args.market, args.fixture)
    if sectors is None:
        sectors = _sectors_frame(args)
        if sectors is None and getattr(args, "offline", False):
            from ashare_hunter.data.debug_fixture import fixture_sectors

            sectors = fixture_sectors(args.market, args.fixture)
    macro = _macro(args)
    owns_provider = provider is None
    if owns_provider:
        provider = _make_provider(args)

    logger.info(
        "CLI 四层研究开始：%s (%s) 市场=%s 离线=%s 场景=%s 板块=%s 宏观=%s",
        stock.get("name"),
        stock.get("code"),
        args.market,
        getattr(args, "offline", False),
        getattr(args, "fixture", "-"),
        0 if sectors is None else len(sectors),
        "已注入" if macro else "未注入",
    )
    started = time.perf_counter()
    # stock = "US.AAPL"

    try:
        result = TradingAgentsOrchestrator(provider).analyze(
            stock,
            market_summary,
            sectors,
            macro=macro,
        )
    finally:
        if owns_provider and provider is not None:
            provider.close()
    logger.info("CLI 四层研究完成：耗时 %.3fs", time.perf_counter() - started)

    json_path = _result_path(args, stock.get("code", "stock"))
    if json_path:
        _write_json(result, json_path)
    _print_analysis(result, detail=args.detail, json_path=json_path, mode=mode)
    return result


def run_scan(args):
    cap = None if args.min_cap is None else args.min_cap * 1e8
    if args.market == MARKET_US:
        from ashare_hunter.graph.us_workflow import USMarketOrchestrator

        orchestrator = USMarketOrchestrator(min_market_cap_usd=cap)
        result = orchestrator.scan(message=lambda msg: logger.info("扫描进度：%s", msg))
    else:
        # 延迟导入：langgraph 在部分环境有依赖限制，放在日志初始化之后便于排查
        from ashare_hunter.graph.workflow import build_workflow

        result = build_workflow(min_market_cap_cny=cap).invoke({})
    _print_scan(result, args.market)

    if args.analyze:
        frame = result.get("top_stocks")
        stock = _find_stock(frame, args.analyze)
        matched = stock is not None
        if not matched:
            logger.warning("代码 %s 未出现在本次扫描候选表中，使用最小股票信息继续研究", args.analyze)
            stock = {
                "code": args.analyze,
                "name": args.analyze,
                "market": "US" if args.market == MARKET_US else "A",
                "currency": "USD" if args.market == MARKET_US else "CNY",
            }
        # scan 子命令的 --analyze 始终使用真实行情；复用 analyze 的参数语义
        scan_args = argparse.Namespace(
            code=args.analyze,
            name=stock.get("name", ""),
            market=args.market,
            offline=False,
            fixture="bull",
            market_json=None,
            sectors_json=None,
            sector=[],
            macro_json=args.macro_json,
            detail=args.detail,
            json_out=args.json_out,
            no_save=args.no_save,
            no_llm=args.no_llm,
            log_level=args.log_level,
            log_file=args.log_file,
        )
        analyze_stock(
            scan_args,
            stock=stock,
            market_summary=result.get("market_summary") or {},
            sectors=result.get("top_sectors"),
            mode=f"扫描({args.market})后联动 · 候选表命中 " + ("是" if matched else "否"),
        )
    elif args.json_out:
        path = Path(args.json_out)
        if path.is_dir() or str(path).endswith(("/", "\\")):
            path = path / f"scan_{args.market}_{datetime.now():%Y%m%d_%H%M%S}.json"
        _write_json(result, path)
        print("扫描结果 JSON:", path)
    return result


def main(argv=None):
    args = parse_args(argv)
    setup_logging(level=args.log_level, log_file=args.log_file)
    started = time.perf_counter()
    logger.info(
        "CLI 启动：命令=%s 日志文件=%s A股最低总市值=%.0f 元",
        args.command,
        current_log_file(),
        settings.a_min_market_cap_cny,
    )
    exit_code = 0
    try:
        with _llm_guard(args):
            if args.command == "analyze":
                mode = f"离线 fixture={args.fixture}" if args.offline else "在线 Futu OpenD"
                analyze_stock(args, mode=mode)
            else:
                run_scan(args)
    except (ValueError, FileNotFoundError) as exc:
        logger.error("CLI 参数错误：%s", exc)
        print(f"参数错误：{exc}")
        exit_code = 2
    except Exception:
        logger.exception("CLI 执行失败")
        print("执行失败，完整堆栈已写入日志：\n" + traceback.format_exc())
        exit_code = 1
    finally:
        logger.info("CLI 结束：耗时 %.3fs 退出码=%d", time.perf_counter() - started, exit_code)
        shutdown_logging()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
