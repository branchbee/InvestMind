import logging
import sys
import time
import traceback
from pathlib import Path

import pandas as pd
from PySide6.QtCore import QObject, QThread, Signal, Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit,
    QFrame, QMessageBox, QHeaderView, QStatusBar, QDialog, QComboBox,
    QDoubleSpinBox
)

from ashare_hunter.agents.stock_analysis import StockAnalysisOrchestrator
from ashare_hunter.agents.trading_agents_orchestrator import TradingAgentsOrchestrator
from ashare_hunter.data.futu_provider import FutuProvider
from ashare_hunter.utils.logging import setup_logging, current_log_file
from config import settings

logger = logging.getLogger(__name__)

MARKET_A = "A"
MARKET_US = "US"
MARKET_LABELS = {MARKET_A: "A股（沪深）", MARKET_US: "美股（全市场）"}


class _GuiLogBridge(QObject):
    """日志 → GUI 的线程安全桥。

    Worker 线程发出的日志通过 Signal 排队到主线程，再写入 QTextEdit，
    避免在子线程直接操作 Qt 控件。
    """

    line = Signal(str)


def display_value(value, column=""):
    if pd.isna(value) if not isinstance(value, (list, dict, tuple)) else False:
        return "-"
    if isinstance(value, float):
        if "amount" in column.lower():
            return f"{value:,.0f}"
        if "pct" in column.lower() or "change" in column.lower() or "return" in column.lower():
            return f"{value:.2f}%"
        return f"{value:.2f}"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _make_provider(us_min_market_cap_usd=None, a_min_market_cap_cny=None):
    """构造 FutuProvider，A 股与美股共用连接参数、各自独立的市值门槛。

    ``us_min_market_cap_usd``（美元）/ ``a_min_market_cap_cny``（人民币元）
    为 GUI 运行时调整值；None 时取 .env 配置。两个门槛分别只作用于
    get_us_spot() / get_spot()，互不干扰。
    """
    return FutuProvider(
        host=settings.futu_host,
        port=settings.futu_port,
        max_plate_refresh_per_run=settings.futu_max_plate_refresh_per_run,
        plate_cache_days=settings.futu_plate_cache_days,
        request_interval=settings.futu_request_interval,
        us_exclude_otc=settings.us_exclude_otc,
        snapshot_batch_size=settings.us_snapshot_batch_size,
        us_min_market_cap=(
            settings.us_min_market_cap_usd if us_min_market_cap_usd is None
            else max(float(us_min_market_cap_usd or 0.0), 0.0)
        ),
        us_missing_market_cap=settings.us_missing_market_cap,
        a_min_market_cap=(
            settings.a_min_market_cap_cny if a_min_market_cap_cny is None
            else max(float(a_min_market_cap_cny or 0.0), 0.0)
        ),
        a_missing_market_cap=settings.a_missing_market_cap,
    )


class MarketScanWorker(QThread):
    """统一扫描 Worker：A 股走 LangGraph 工作流，美股走板块扫描编排器。"""

    completed = Signal(object)
    failed = Signal(str)
    message = Signal(str)

    def __init__(self, market=MARKET_A, min_market_cap=None, missing_market_cap=None):
        super().__init__()
        self.market = market
        # 门槛单位随市场：A 股为人民币元，美股为美元
        self.min_market_cap = min_market_cap
        self.missing_market_cap = missing_market_cap

    def run(self):
        provider = None
        started = time.perf_counter()
        market = "US" if self.market == MARKET_US else "A"
        logger.info("扫描 Worker 启动：市场=%s 市值门槛=%s 缺失策略=%s",
                    market, self.min_market_cap, self.missing_market_cap)
        try:
            if self.market == MARKET_US:
                from ashare_hunter.graph.us_workflow import USMarketOrchestrator
                provider = _make_provider(us_min_market_cap_usd=self.min_market_cap)
                result = USMarketOrchestrator(
                    provider, min_market_cap_usd=self.min_market_cap,
                    missing_market_cap=self.missing_market_cap,
                ).scan(message=self.message.emit)
            else:
                # 延迟导入：避免在缺少 langgraph 依赖时 GUI 无法启动
                self.message.emit("正在连接 Futu OpenD 并获取行情……")
                from ashare_hunter.graph.workflow import build_workflow
                result = build_workflow(
                    min_market_cap_cny=self.min_market_cap,
                    missing_market_cap=self.missing_market_cap,
                ).invoke({})
            logger.info("扫描 Worker 完成：市场=%s 耗时 %.3fs",
                        market, time.perf_counter() - started)
            self.completed.emit(result)
        except Exception:
            error = traceback.format_exc()
            logger.error("扫描 Worker 失败：市场=%s 耗时 %.3fs\n%s",
                         market, time.perf_counter() - started, error)
            self.failed.emit(error)
        finally:
            if provider is not None:
                provider.close()


class TradingAnalysisWorker(QThread):
    """统一四层研究 Worker：A 股 / 美股共用 TradingAgentsOrchestrator。"""

    completed = Signal(object)
    failed = Signal(str)
    message = Signal(str)

    def __init__(self, stock, market_summary, sectors, market=MARKET_A):
        super().__init__()
        self.stock = stock
        self.market_summary = market_summary
        self.sectors = sectors
        self.market = market

    def run(self):
        provider = None
        started = time.perf_counter()
        name = self.stock.get('name', '')
        code = self.stock.get('code', '')
        market = "US" if self.market == MARKET_US else "A"
        logger.info("四层研究 Worker 启动：%s (%s) 市场=%s", name, code, market)
        try:
            scope = '美股' if self.market == MARKET_US else ''
            self.message.emit(
                f"正在对 {name} ({code}) 执行 {scope}TradingAgents 四层研究……"
            )
            provider = _make_provider()
            stock = dict(self.stock)
            if self.market == MARKET_US:
                stock.setdefault('market', 'US')
                stock.setdefault('currency', 'USD')
            result = TradingAgentsOrchestrator(provider).analyze(
                stock, self.market_summary, self.sectors
            )
            logger.info("四层研究 Worker 完成：%s (%s) 耗时 %.3fs",
                        name, code, time.perf_counter() - started)
            self.completed.emit(result)
        except Exception:
            error = traceback.format_exc()
            logger.error("四层研究 Worker 失败：%s (%s) 耗时 %.3fs\n%s",
                         name, code, time.perf_counter() - started, error)
            self.failed.emit(error)
        finally:
            if provider is not None:
                provider.close()


class StockAnalysisDialog(QDialog):
    """分层展示 TradingAgents 四层研究结果。"""

    def __init__(self, result, parent=None):
        super().__init__(parent)
        stock = result.get("stock", {})
        self.setWindowTitle(f"TradingAgents 个股研究 · {stock.get('name', '')} {stock.get('code', '')}")
        self.resize(1120, 840)
        layout = QVBoxLayout(self)

        heading = QLabel(
            f"{stock.get('name', '未知股票')}（{stock.get('code', '')}）  "
            f"当前涨跌幅：{display_value(stock.get('change_pct'), 'change_pct')}"
        )
        heading.setStyleSheet("font-size: 20px; font-weight: 700; padding: 8px;")
        layout.addWidget(heading)

        ta = result.get("trading_agents") or {}
        final = ta.get("final_recommendation") or {}
        action = final.get("action") or "待判断"
        action_color = {"买入": "#c62828", "卖出": "#2e7d32", "持有": "#616161"}.get(action, "#244b87")
        banner = QLabel(
            f"最终建议：{action}　｜　研究裁决：{final.get('verdict', '—')}　｜　"
            f"建议仓位：{final.get('position', '—')}　｜　"
            f"风险等级：{final.get('risk_level', '—')}　｜　策略审核：{final.get('review', '—')}"
        )
        banner.setStyleSheet(
            f"background:#f5f8fd; color:{action_color}; padding:12px; border-radius:8px; "
            "font-size:16px; font-weight:700;"
        )
        layout.addWidget(banner)

        tabs = QTabWidget()
        tabs.addTab(self._text(self._overview(result)), "总览")
        tabs.addTab(self._text(self._analysts(result)), "第一层 · 分析师")
        if ta:
            tabs.addTab(self._text(self._debate(ta.get("research") or {})), "第二层 · 研究辩论")
            tabs.addTab(self._text(self._trading(ta.get("trading") or {})), "第三层 · 交易决策")
            tabs.addTab(self._text(self._risk(ta.get("risk") or {})), "第四层 · 风险管理")
        tabs.addTab(
            self._text("## AI 研究总结\n\n" + str(result.get("llm_summary", "未生成"))), "AI 总结"
        )
        layout.addWidget(tabs, 1)

        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button, alignment=Qt.AlignRight)

    @staticmethod
    def _text(markdown):
        widget = QTextEdit()
        widget.setReadOnly(True)
        widget.setMarkdown(markdown)
        return widget

    @staticmethod
    def _overview(result):
        overall = result.get("overall") or {}
        score = overall.get("score")
        history_diag = (result.get("data_sources") or {}).get("history") or {}
        history_status = (
            "可用" if history_diag.get("available", True)
            else f"不可用：{history_diag.get('reason') or '原因未记录'}"
        )
        lines = [
            "## 综合结论",
            f"**{overall.get('label', '待判断')}**",
            "",
            str(overall.get("explanation", "")),
            "",
            f"- 综合评分：{f'{score:.1f}' if isinstance(score, (int, float)) else '—'} / 100",
            f"- 维度覆盖：{overall.get('coverage', 0):.0%}",
            f"- 置信度：{overall.get('confidence', '低')}",
            f"- 最终操作建议：{overall.get('final_action', '—')}",
            f"- 建议仓位：{overall.get('final_position', '—')}",
            "",
            "## 投资逻辑",
        ]
        logic = overall.get("investment_logic") or []
        lines += [f"- {x}" for x in logic] if logic else ["- 暂无可列示的量化依据。"]
        lines += ["", "## 风险因素"]
        risks = overall.get("risk_factors") or []
        lines += [f"- {x}" for x in risks] if risks else ["- 暂无特定风险阈值触发。"]
        lines += [
            "",
            f"- 日线数据条数：{result.get('history_rows', 0)}",
            f"- 最新日线日期：{result.get('history_latest_date', '未知')}",
            f"- 日线数据状态：{history_status}",
            "",
            "## 数据边界与风险提示",
            "- 技术指标由 Futu 日线数据计算；财务、估值、公告、资金数据来自 Futu 对应接口，缺失项均明确标记，未使用虚构数值。",
            "- 多空辩论、交易建议与仓位评估均为规则化研究信号，不构成投资建议；卖出对空仓者表示回避。",
            "- 市场有风险，投资需谨慎。任何决策应结合个人风险承受能力独立判断。",
        ]
        return "\n".join(lines)

    @staticmethod
    def _analysts(result):
        lines = ["## 第一层 · 分析师团队"]
        diag = (result.get("data_sources") or {}).get("history") or {}
        if diag and not diag.get("available", True):
            lines += [
                "",
                "> **日线数据不可用** — 趋势 / 动量 Agent 与技术面证据因此标记「数据不足」",
                f"> 原因：{diag.get('reason') or '未提供'}",
                f"> 返回 {diag.get('rows', 0)} 行，有效收盘 {diag.get('valid_close_rows', 0)} 根"
                "（趋势 Agent 至少需要 20 根）。",
                "> 排查方向：Futu OpenD 连接、历史 K 线行情权限、标的退市/停牌或次新股、接口限频；"
                "详情见「运行日志」与日志文件。",
            ]
        for agent in result.get("agents", []):
            score = agent.get("score")
            score_text = f" · 评分 {score:.1f}" if isinstance(score, (int, float)) else ""
            lines += [
                "",
                f"### {agent.get('agent', 'Agent')} · {agent.get('status', '')}{score_text}",
                agent.get("conclusion", "暂无结论"),
            ]
            for key, value in agent.items():
                if key in ("agent", "status", "conclusion", "risk_flags", "matched_sectors",
                           "items", "top_tags", "dimensions", "evidence", "arguments", "rounds") or value is None:
                    continue
                if isinstance(value, (int, float, str, bool)):
                    lines.append(f"- {key}: {value}")
            for key, label in (("investment_logic", "投资逻辑"), ("risk_factors", "风险因素")):
                if agent.get(key):
                    lines.append(f"- {label}：" + "；".join(str(x) for x in agent[key]))
            if agent.get("risk_flags"):
                lines.append("- 风险提示：" + "；".join(agent["risk_flags"]))
            if agent.get("matched_sectors"):
                lines.append("- 匹配板块：" + "；".join(x["sector_name"] for x in agent["matched_sectors"]))
        return "\n".join(lines)

    @staticmethod
    def _debate(debate):
        bull = debate.get("bull_case") or {}
        bear = debate.get("bear_case") or {}
        lines = [
            "## 第二层 · 多空研究辩论",
            "",
            "### 多头研究员",
            str(bull.get("summary") or "暂无可核验证据。"),
        ]
        lines += [f"- {e['text']}（{e['source']}·{e['strength']}）" for e in bull.get("arguments", [])]
        lines += ["", "### 空头研究员", str(bear.get("summary") or "暂无可核验证据。")]
        lines += [f"- {e['text']}（{e['source']}·{e['strength']}）" for e in bear.get("arguments", [])]

        matrix = debate.get("evidence_matrix") or []
        cov = debate.get("evidence_coverage") or {}
        if matrix:
            lines += [
                "", "### 四层证据检查表",
                f"可判定 {cov.get('answered', 0)}/{cov.get('total', 0)} 项；"
                "「数据不足」项不参与多空计分，也不生成推断性结论。",
            ]
            by_layer = debate.get("evidence_by_layer") or {}
            for layer in ("基本面", "技术面", "新闻面", "宏观面"):
                rows = by_layer.get(layer) or []
                if not rows:
                    continue
                stat = (cov.get("by_layer") or {}).get(layer) or {}
                lines += ["", f"#### {layer}（{stat.get('answered', 0)}/{stat.get('total', 0)} 可判定）"]
                for item in rows:
                    mark = {"bull": "多头", "bear": "空头", "neutral": "中性",
                            "unknown": "待补数据"}.get(item.get("direction"), "—")
                    lines.append(
                        f"- **[{item.get('answer')}｜{mark}｜{item.get('strength') or '—'}]** "
                        f"{item.get('question')} — {item.get('detail')} "
                        f"（来源：{item.get('source')}；数据时间：{item.get('as_of')}；"
                        f"依据：{item.get('ref')}）"
                    )
        gaps = debate.get("unresolved_questions") or []
        if gaps:
            lines += ["", "### 尚缺数据（不参与裁决）"]
            lines += [f"- {q}" for q in gaps]
        caveats = debate.get("caveats") or []
        if caveats:
            lines += ["", "### 审慎提示"]
            lines += [f"- {x}" for x in caveats]

        for rnd in debate.get("rounds", []):
            lines += ["", f"### 第 {rnd.get('round')} 轮"]
            bull_side = rnd.get("bull") or {}
            bear_side = rnd.get("bear") or {}
            if bull_side:
                lines += [f"**多头陈述**：{bull_side.get('statement', '')}"]
                lines += [f"- {x}" for x in bull_side.get("arguments", [])]
            if bear_side:
                lines += [f"**空头陈述**：{bear_side.get('statement', '')}"]
                lines += [f"- {x}" for x in bear_side.get("arguments", [])]
            for layer_view in rnd.get("layers") or []:
                lines += ["", f"**{layer_view.get('layer')}**：多头 "
                          f"{layer_view.get('bull_count', 0)} 条 / 空头 "
                          f"{layer_view.get('bear_count', 0)} 条"]
                lines += [f"  - 多：{x}" for x in layer_view.get("bull") or []]
                lines += [f"  - 空：{x}" for x in layer_view.get("bear") or []]
            if rnd.get("bull_rebuttal"):
                lines += [
                    f"**多头反驳**：{rnd['bull_rebuttal']}",
                    f"**空头反驳**：{rnd.get('bear_rebuttal', '')}",
                ]
        judge = debate.get("judge") or {}
        lines += ["", "### 研究主管裁决", f"**{judge.get('verdict', '—')}**", str(judge.get("reasoning", ""))]
        plan = debate.get("investment_plan") or {}
        lines += [
            "", "### 投资计划",
            f"- 核心假设：{plan.get('thesis', '—')}",
            f"- 时间维度：{plan.get('time_horizon', '—')}",
            "- 入场观察条件：" + "；".join(plan.get("entry_conditions") or []),
            "- 退出观察条件：" + "；".join(plan.get("exit_conditions") or []),
        ]
        return "\n".join(lines)

    @staticmethod
    def _trading(trader):
        lines = [
            "## 第三层 · 交易决策",
            "",
            f"### {trader.get('action', '待判断')}（{trader.get('action_code', '—')}）"
            f"　置信度：{trader.get('confidence', '—')}",
            "",
            f"- 研究裁决：{trader.get('verdict', '—')}",
            f"- 决策依据：{trader.get('reason', '—')}",
            f"- 关注区间：{trader.get('entry_zone', '—')}",
            f"- 时间维度：{trader.get('time_horizon', '—')}",
            "",
            "### 交易逻辑",
            str(trader.get("thesis", "")),
            "",
            "### 失效条件",
        ]
        lines += [f"- {x}" for x in trader.get("invalidation", [])]
        lines += ["", "### 说明", str(trader.get("conclusion", ""))]
        return "\n".join(lines)

    @staticmethod
    def _risk(risk):
        lines = [
            "## 第四层 · 风险管理",
            "",
            f"### 风险等级：{risk.get('risk_level', '—')}（{risk.get('risk_score', '—')}/100）",
            "",
            "## 风险检查项",
        ]
        checks = risk.get("risk_checks") or []
        lines += [f"- **{c['name']}（{c['level']}）**：{c['detail']}" for c in checks] if checks else ["- 未触发可核验的风险阈值"]
        debate = risk.get("risk_debate") or {}
        lines += ["", "## 三方风险观点"]
        for key, label in (("aggressive", "激进视角"), ("conservative", "保守视角"), ("neutral", "中性视角")):
            if debate.get(key):
                lines += [f"### {label}", str(debate[key]), ""]
        position = risk.get("position_size") or {}
        lines += [
            "## 仓位评估",
            f"- 建议仓位：{position.get('recommended', '—')}",
            f"- 仓位上限：{position.get('max_pct', '—')}%",
            f"- 测算依据：{position.get('basis', '—')}",
            "",
            "## 策略审核",
            f"- 审核结果：{(risk.get('strategy_review') or {}).get('result', '—')}",
        ]
        lines += [f"- {x}" for x in (risk.get("strategy_review") or {}).get("reasons", [])]
        final = risk.get("final_decision") or {}
        lines += ["", "## 最终执行决策",
                  f"- 操作：{final.get('action', '—')}",
                  f"- 仓位：{final.get('position', '—')}",
                  f"- 审核：{final.get('review', '—')}",
                  "- 附加条件："]
        lines += [f"  - {x}" for x in final.get("conditions", [])]
        lines += ["", str(risk.get("conclusion", ""))]
        return "\n".join(lines)


class MetricCard(QFrame):
    def __init__(self, title, value="—", hint=""):
        super().__init__()
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        self.title = QLabel(title)
        self.title.setObjectName("metricTitle")
        self.value = QLabel(value)
        self.value.setObjectName("metricValue")
        self.hint = QLabel(hint)
        self.hint.setObjectName("metricHint")
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.hint)

    def set_value(self, value, hint=None):
        self.value.setText(str(value))
        if hint is not None:
            self.hint.setText(hint)


class HunterWindow(QMainWindow):
    """主窗口：通过市场下拉框在 A 股 / 美股之间切换，共用同一套界面。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Market Hunter · 多市场板块/个股研究")
        self.resize(1420, 900)
        self.worker = None
        self.analysis_worker = None
        self.analysis_dialog = None
        self.result = None
        # 深度研究按钮的状态机：idle / scanning / no_result / ready / busy
        self._analyze_state = "idle"
        self._analysis_started = None
        self._analysis_tick = 0
        self._build_ui()
        self._analysis_timer = QTimer(self)
        self._analysis_timer.setInterval(400)
        self._analysis_timer.timeout.connect(self._tick_analysis_progress)
        self._apply_style()
        self._on_market_changed()  # 初始化市场相关文案

    # ---------- UI 构建 ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(10)
        title_box = QVBoxLayout()
        title = QLabel("Market Hunter")
        title.setObjectName("mainTitle")
        self.subtitle = QLabel("基于 Futu OpenD + TradingAgents 的多市场投研系统")
        self.subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(self.subtitle)
        header.addLayout(title_box)
        header.addStretch()

        header.addWidget(QLabel("市场："))
        self.market_combo = QComboBox()
        for market, label in MARKET_LABELS.items():
            self.market_combo.addItem(label, market)
        self.market_combo.setObjectName("marketCombo")
        self.market_combo.currentIndexChanged.connect(self._on_market_changed)
        header.addWidget(self.market_combo)

        # 市值门槛：A股/美股通用，单位随市场切换（亿元 / 亿美元），各自记忆阈值
        self.cap_label = QLabel("最低市值：")
        header.addWidget(self.cap_label)
        self.min_cap_spin = QDoubleSpinBox()
        self.min_cap_spin.setObjectName("minCapSpin")
        self.min_cap_spin.setRange(0.0, 100000.0)
        self.min_cap_spin.setDecimals(1)
        self.min_cap_spin.setSingleStep(1.0)
        self._cap_values = {
            MARKET_A: max(settings.a_min_market_cap_cny / 1e8, 0.0),
            MARKET_US: max(settings.us_min_market_cap_usd / 1e8, 0.0),
        }
        self.min_cap_spin.setValue(self._cap_values[MARKET_A])
        self.min_cap_spin.valueChanged.connect(self._on_cap_value_changed)
        self._apply_cap_unit(MARKET_A)
        header.addWidget(self.min_cap_spin)

        self.scan_button = QPushButton("▶  开始扫描")
        self.scan_button.setObjectName("scanButton")
        self.scan_button.clicked.connect(self.start_scan)
        header.addWidget(self.scan_button)
        self.analyze_button = QPushButton("深度研究 · 待扫描")
        self.analyze_button.setObjectName("analyzeButton")
        # 固定宽度：状态文案和标的名切换时工具栏不跳动、文字不被裁切
        self.analyze_button.setFixedWidth(224)
        self.analyze_button.setEnabled(False)
        # QPushButton.setText 会按 & 助记符重置 setShortcut，导致状态文案一变就丢快捷键。
        # 快捷键改挂窗口级 QShortcut，主键盘回车与小键盘回车都支持。
        self._analyze_shortcuts = [QShortcut(QKeySequence(seq), self) for seq in ("Ctrl+Return", "Ctrl+Enter")]
        for shortcut in self._analyze_shortcuts:
            shortcut.activated.connect(self.analyze_selected_stock)
            shortcut.setEnabled(False)
        self.analyze_button.setToolTip(
            "请先完成一次市场扫描，再选择一只候选个股进行 TradingAgents 四层研究；快捷键：Ctrl+Enter"
        )
        self.analyze_button.clicked.connect(self.analyze_selected_stock)
        header.addWidget(self.analyze_button)
        root.addLayout(header)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self.status_label = QLabel("尚未扫描。")
        self.status_label.setObjectName("statusLabel")
        self.selection_hint = QLabel("当前标的：—")
        self.selection_hint.setObjectName("selectionHint")
        self.selection_hint.setMinimumWidth(230)
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.selection_hint)
        root.addLayout(status_row)

        cards = QHBoxLayout()
        self.card_regime = MetricCard("市场环境", "—", "等待扫描")
        self.card_breadth = MetricCard("上涨家数占比", "—", "上涨 / 下跌")
        self.card_avg = MetricCard("全市场平均涨跌幅", "—", "快照统计")
        self.card_amount = MetricCard("快照成交额", "—", "按 Provider 返回字段汇总")
        self.card_sector = MetricCard("强势板块", "0", "入选数量")
        self.card_stock = MetricCard("候选个股", "0", "入选数量")
        for card in [self.card_regime, self.card_breadth, self.card_avg,
                     self.card_amount, self.card_sector, self.card_stock]:
            cards.addWidget(card)
        root.addLayout(cards)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self.market_text = QTextEdit()
        self.market_text.setReadOnly(True)
        self.tabs.addTab(self.market_text, "市场概览")

        self.sector_table = self._make_table()
        self.tabs.addTab(self.sector_table, "强势板块")
        self.sector_tab_index = self.tabs.indexOf(self.sector_table)

        self.stock_table = self._make_table()
        self.stock_table.cellDoubleClicked.connect(self.analyze_selected_stock)
        self.stock_table.itemSelectionChanged.connect(self._on_stock_selection_changed)
        self.tabs.addTab(self.stock_table, "候选个股")

        self.explanation = QTextEdit()
        self.explanation.setReadOnly(True)
        self.tabs.addTab(self.explanation, "Agent 研究解读")

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        # DEBUG 模式下日志量较大，限制面板最大行数避免内存无限增长
        self.log_text.document().setMaximumBlockCount(2000)
        self.tabs.addTab(self.log_text, "运行日志")

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("就绪")

    def _make_table(self):
        table = QTableWidget()
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSortingEnabled(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        return table

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #f4f7fb; color: #172033; font-family: "Microsoft YaHei", "Arial"; font-size: 13px; }
            QLabel#mainTitle { font-size: 27px; font-weight: 700; color: #14213d; }
            QLabel#subtitle { color: #667085; font-size: 12px; }
            QLabel#statusLabel { background: #eaf1fb; color: #31527a; border-radius: 8px; padding: 10px 12px; }
            QFrame#metricCard { background: white; border: 1px solid #e4e9f1; border-radius: 12px; }
            QLabel#metricTitle { color: #667085; font-size: 12px; }
            QLabel#metricValue { color: #172033; font-size: 23px; font-weight: 700; }
            QLabel#metricHint { color: #98a2b3; font-size: 11px; }
            QComboBox#marketCombo { background: white; border: 1px solid #d0d9e8; border-radius: 8px; padding: 8px 12px; min-width: 130px; font-weight: 600; }
            QDoubleSpinBox#minCapSpin { background: white; border: 1px solid #d0d9e8; border-radius: 8px; padding: 8px 12px; min-width: 130px; }
            QPushButton#scanButton { background: #2457d6; color: white; border: 0; border-radius: 8px; padding: 11px 20px; font-weight: 600; }
            QPushButton#scanButton:hover { background: #1948bd; }
            QPushButton#scanButton:disabled { background: #9aabd2; }
            /* 深度研究：深色渐变作为投研主操作，与蓝色扫描按钮形成主次对比 */
            QPushButton#analyzeButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1b2550, stop:1 #4356c9);
                color: #ffffff; border: 0; border-radius: 8px;
                padding: 11px 14px; font-weight: 600;
            }
            QPushButton#analyzeButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2b3a7d, stop:1 #5a70e6);
            }
            QPushButton#analyzeButton:pressed { background: #141d42; padding-top: 12px; padding-bottom: 10px; }
            QPushButton#analyzeButton:disabled { background: #e9eef6; color: #98a2b3; border: 1px dashed #c9d5e8; }
            QPushButton#analyzeButton[state="busy"] {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6a3fb5, stop:1 #9a5ad8);
                color: #ffffff; border: 1px solid #8a4cc9;
            }
            QLabel#selectionHint { background: #ffffff; color: #31527a; border: 1px solid #dfe6f2; border-radius: 8px; padding: 10px 12px; font-weight: 600; }
            QToolTip { background: #1f2a44; color: #ffffff; border: 0; padding: 6px 9px; border-radius: 5px; }
            QTabWidget::pane { border: 1px solid #e3e8f0; background: white; border-radius: 8px; }
            QTabBar::tab { background: #e9eef6; padding: 10px 18px; margin-right: 4px; border-top-left-radius: 7px; border-top-right-radius: 7px; }
            QTabBar::tab:selected { background: white; color: #2457d6; font-weight: 600; }
            QTableWidget, QTextEdit { background: white; border: 1px solid #e4e9f1; border-radius: 7px; gridline-color: #edf0f5; }
            QTableWidget::item:selected { background: #d9e5ff; color: #14213d; }
            QHeaderView::section { background: #f2f5fa; color: #475467; border: 0; padding: 8px; font-weight: 600; }
        """)

    # ---------- 深度研究按钮状态机 ----------
    _ANALYZE_TIPS = {
        "idle": "请先点击「开始扫描」，扫描完成后再选择一只候选个股进行 TradingAgents 四层研究。快捷键：Ctrl+Enter",
        "scanning": "市场扫描进行中，扫描完成后才能对候选个股做深度研究。快捷键：Ctrl+Enter",
        "no_result": "本次扫描没有产生候选个股，请调低最低市值门槛或稍后重试。快捷键：Ctrl+Enter",
        "busy": "TradingAgents 四层研究正在后台运行，请等待当前任务完成。",
    }

    def _set_analyze_state(self, state, *, code="", name=""):
        """统一按钮文案 / 可用性 / 配色 / 提示，避免各处 setEnabled 造成状态漂移。"""
        self._analyze_state = state
        for shortcut in getattr(self, "_analyze_shortcuts", ()):
            shortcut.setEnabled(state == "ready")
        button = self.analyze_button
        button.setProperty("state", "busy" if state == "busy" else "normal")
        # Qt 样式表不会自动追踪动态属性变化，需重新 polish 才能切换配色
        button.style().unpolish(button)
        button.style().polish(button)
        button.update()

        if state == "busy":
            # 保持可点击以显示醒目的「研究中」状态；重复触发在槽入口统一拦截
            button.setEnabled(True)
            button.setCursor(Qt.CursorShape.WaitCursor)
            button.setText("研究中 · 0s")
            button.setToolTip(self._ANALYZE_TIPS["busy"])
            self.selection_hint.setText(f"当前标的：{name}（{code}） · 研究中" if code else "当前标的：研究中")
            return

        button.setEnabled(state == "ready")
        button.setCursor(Qt.CursorShape.PointingHandCursor if state == "ready" else Qt.CursorShape.ArrowCursor)
        if state == "ready":
            self._refresh_ready_button()
            return
        button.setText(
            {
                "idle": "深度研究 · 待扫描",
                "scanning": "深度研究 · 扫描中",
                "no_result": "深度研究 · 无候选",
            }.get(state, "深度研究")
        )
        button.setToolTip(self._ANALYZE_TIPS.get(state, ""))
        self.selection_hint.setText("当前标的：—")

    def _candidate_frame(self):
        frame = (self.result or {}).get("top_stocks", pd.DataFrame())
        return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)

    def _selected_stock_brief(self):
        """返回 (code, name)；代码取自 UserRole，避免表格格式化与排序干扰。"""
        table = self.stock_table
        row = table.currentRow()
        if row < 0 or row >= table.rowCount():
            return "", ""
        code_col = next(
            (
                c
                for c in range(table.columnCount())
                if table.horizontalHeaderItem(c) and table.horizontalHeaderItem(c).text() == "code"
            ),
            -1,
        )
        if code_col < 0:
            return "", ""
        item = table.item(row, code_col)
        if item is None:
            return "", ""
        code = str(item.data(Qt.UserRole) or item.text() or "")
        name = ""
        frame = self._candidate_frame()
        if code and {"code", "name"}.issubset(frame.columns):
            matched = frame[frame["code"].astype(str) == code]
            if not matched.empty:
                name = str(matched.iloc[0].get("name", "") or "")
        return code, name

    def _refresh_ready_button(self):
        """把按钮作用对象写在按钮上，避免「点了不知道会分析哪只」。"""
        code, name = self._selected_stock_brief()
        label = f"{name}（{code}）" if code and name else code
        if code:
            self.analyze_button.setText(f"深度研究 · {code}")
            self.analyze_button.setToolTip(
                f"对 {label} 执行 TradingAgents 四层研究：分析师团队 → 多空辩论 → "
                "交易决策 → 风险管理。快捷键：Ctrl+Enter"
            )
            self.selection_hint.setText(f"当前标的：{label}")
        else:
            self.analyze_button.setText("深度研究 · 选标的")
            self.analyze_button.setToolTip(
                "请在「候选个股」表格中选择一只标的，或双击该行；"
                "直接点击本按钮将默认分析第一只候选股。快捷键：Ctrl+Enter"
            )
            self.selection_hint.setText("当前标的：未选择（点击默认分析首只）")

    def _on_stock_selection_changed(self):
        if self._analyze_state == "ready":
            self._refresh_ready_button()

    def _tick_analysis_progress(self):
        """研究期间按秒刷新按钮与状态栏，替代原来「点了就灰掉」的无反馈状态。"""
        if self._analyze_state != "busy" or self._analysis_started is None:
            self._analysis_timer.stop()
            return
        self._analysis_tick += 1
        elapsed = time.perf_counter() - self._analysis_started
        dots = "·" * (self._analysis_tick % 3 + 1)
        self.analyze_button.setText(f"研究中 {dots} {elapsed:.0f}s")
        self.statusBar().showMessage(f"TradingAgents 四层研究进行中…… 已用时 {elapsed:.0f}s")

    def _on_analysis_finished(self):
        """由 QThread.finished 触发：线程真正结束才解锁，避免覆盖仍在运行的 Worker。"""
        self._analysis_timer.stop()
        self._analysis_started = None
        worker = self.analysis_worker
        self.analysis_worker = None
        if worker is not None:
            worker.deleteLater()
        # 即使状态被市场切换等路径提前重置，也必须恢复这两个入口，避免界面卡死
        scan_busy = self.worker is not None and self.worker.isRunning()
        self.scan_button.setEnabled(not scan_busy)
        self.market_combo.setEnabled(not scan_busy)
        if self._analyze_state != "busy":
            return
        if not self._candidate_frame().empty:
            self._set_analyze_state("ready")
        else:
            self._set_analyze_state("idle" if self.result is None else "no_result")
        self.statusBar().showMessage("研究任务已结束", 5000)

    # ---------- 市场切换 ----------
    def current_market(self):
        return self.market_combo.currentData()

    def is_us(self):
        return self.current_market() == MARKET_US

    def min_market_cap(self):
        """当前市场最低总市值门槛：A 股为人民币元，美股为美元。"""
        return float(self.min_cap_spin.value()) * 1e8

    def _on_cap_value_changed(self, value):
        """记录当前市场的门槛，切换市场时各自恢复，互不覆盖。"""
        self._cap_values[self.current_market()] = float(value)

    def _apply_cap_unit(self, market):
        us = market == MARKET_US
        self.min_cap_spin.setSuffix(" 亿美元" if us else " 亿元")
        self.min_cap_spin.setToolTip(
            f"低于该总市值的{'美股' if us else 'A 股'}在快照阶段即被剔除，"
            "不参与板块聚合、选股与深度研究；设为 0 关闭市值过滤。"
            f"口径为富途快照 total_market_val（{'美元' if us else '人民币元'}）。"
        )

    def _on_market_changed(self):
        """市场切换：更新文案、市值单位与各自门槛，并清空上次结果。"""
        us = self.is_us()
        market = self.current_market()
        logger.info("市场切换为 %s，清空上次扫描结果", "美股（全市场）" if us else "A股（沪深）")
        # 先阻塞 valueChanged 再恢复该市场自己的阈值，避免把 A 股值写入美股记忆
        self.min_cap_spin.blockSignals(True)
        self.min_cap_spin.setValue(self._cap_values.get(market, 0.0))
        self.min_cap_spin.blockSignals(False)
        self._apply_cap_unit(market)
        self.subtitle.setText(
            "美股全市场板块扫描 + TradingAgents 四层投研流水线（美元计价）" if us
            else "基于 Futu OpenD + TradingAgents 的 A 股板块/个股发现"
        )
        self.scan_button.setText("▶  扫描美股全市场" if us else "▶  扫描A股全市场")
        self.card_amount.hint.setText("美元口径汇总" if us else "按 Provider 返回字段汇总")
        self.status_label.setText(
            "美股模式：请启动 Futu OpenD 并登录具备美股行情与板块权限的账号。"
            if us else
            "A股模式：请启动 Futu OpenD 并登录具备 A股行情权限的账号。"
        )
        self._clear_results()

    def _clear_results(self):
        self.result = None
        self._set_analyze_state("idle")
        for card in (self.card_regime, self.card_breadth, self.card_avg,
                     self.card_amount, self.card_sector, self.card_stock):
            card.set_value("—")
        for table in (self.sector_table, self.stock_table):
            table.setSortingEnabled(False)
            table.clear()
            table.setRowCount(0)
            table.setColumnCount(0)
            table.setSortingEnabled(True)
        self.market_text.clear()
        self.explanation.clear()

    # ---------- 扫描 ----------
    def start_scan(self):
        self.scan_button.setEnabled(False)
        self.market_combo.setEnabled(False)
        self._set_analyze_state("scanning")
        min_cap = self.min_market_cap()
        cap_unit = "亿美元" if self.is_us() else "亿元"
        cap_text = (
            f"（最低总市值 {min_cap / 1e8:.1f} {cap_unit}）" if min_cap > 0
            else "（未启用市值过滤）"
        )
        self.status_label.setText(
            f"{'美股全市场' if self.is_us() else 'A股全市场'}"
            f"扫描运行中{cap_text}：正在连接行情服务并计算板块与个股强度……"
        )
        self.log_text.append(
            f"开始执行{'美股' if self.is_us() else 'A股'}全市场板块扫描 {cap_text}。"
        )
        self.statusBar().showMessage("扫描中……")
        self.worker = MarketScanWorker(
            market=self.current_market(),
            min_market_cap=min_cap,
            missing_market_cap=(
                settings.us_missing_market_cap if self.is_us()
                else settings.a_missing_market_cap
            ),
        )
        self.worker.message.connect(self.on_message)
        self.worker.completed.connect(self.on_completed)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def append_log_line(self, text):
        """日志桥的槽：把日志追加到「运行日志」面板（由主线程执行）。"""
        self.log_text.append(text)

    def on_message(self, message):
        self.status_label.setText(message)
        self.log_text.append(message)

    def on_completed(self, result):
        self.result = result
        self.scan_button.setEnabled(True)
        self.market_combo.setEnabled(True)
        us = self.is_us()
        currency = "USD" if us else "CNY"
        unit = "亿美元" if us else "亿"
        market = result.get("market_summary", {})
        sectors = result.get("top_sectors", pd.DataFrame())
        stocks = result.get("top_stocks", pd.DataFrame())
        if not isinstance(sectors, pd.DataFrame):
            sectors = pd.DataFrame(sectors)
        if not isinstance(stocks, pd.DataFrame):
            stocks = pd.DataFrame(stocks)

        breadth = market.get("advance_decline_ratio")
        self.card_regime.set_value(market.get("market_regime", "未知"), "市场宽度与平均涨跌幅综合判断")
        self.card_breadth.set_value(
            f"{breadth:.1%}" if isinstance(breadth, (float, int)) else "—",
            f"上涨 {market.get('up_count', 0)} / 下跌 {market.get('down_count', 0)}",
        )
        self.card_avg.set_value(f"{market.get('avg_change_pct', 0):.2f}%", "快照平均涨跌幅")
        amount = market.get("total_amount_yuan", 0)
        self.card_amount.set_value(f"{amount / 1e8:.2f} {unit}", "快照成交额汇总")
        self.card_sector.set_value(len(sectors), "量化评分靠前的正涨幅板块")
        self.card_stock.set_value(len(stocks), "通过市值/成交额/涨幅过滤并排序")

        # 两个市场都展示强势板块 Tab
        self._populate_table(self.sector_table, sectors, currency)
        self._populate_table(self.stock_table, stocks, currency)
        if stocks.empty:
            self._set_analyze_state("no_result")
        else:
            self._set_analyze_state("ready")
            # 自动选中首只：按钮立即显示待研究的代码，点击不再弹「请先选择」
            self.stock_table.selectRow(0)

        lines = [
            f"扫描时间：{result.get('scan_time', '未知')}",
            f"市场：{'美股（全市场，USD）' if us else 'A股（沪深，CNY）'}",
            f"市场环境：{market.get('market_regime', '未知')}",
            f"上涨 / 下跌 / 平盘：{market.get('up_count', 0)} / {market.get('down_count', 0)} / {market.get('flat_count', 0)}",
            f"上涨家数占比：{market.get('advance_decline_ratio', 0):.1%}",
            f"平均涨跌幅：{market.get('avg_change_pct', 0):.3f}%",
            f"强势板块：{len(sectors)} 个",
        ]
        if us:
            lines.append(f"成交额口径：{market.get('amount_note', '美元')}")
        else:
            lines.append(f"报告文件：{result.get('report_path', '未生成')}")
        cap = result.get("market_cap_filter") or {}
        if cap.get("enabled"):
            cap_unit = "亿美元" if us else "亿元"
            threshold = float(cap.get("threshold", cap.get("threshold_usd", 0)) or 0)
            lines.append(
                f"市值门槛：{threshold / 1e8:.1f} {cap_unit}；"
                f"过滤前 {cap.get('before', 0)} 只 → 保留 {cap.get('after', 0)} 只"
                f"（低于门槛剔除 {cap.get('below', 0)} 只；市值缺失 "
                f"{cap.get('missing', 0)} 只，按 {cap.get('missing_policy', 'keep')} "
                f"策略剔除 {cap.get('missing_excluded', 0)} 只）"
            )
            if not cap.get("has_field"):
                lines.append("注意：本次快照未提供总市值字段，市值门槛未实际生效。")
        lines += [
            "",
            "说明：板块强度由成分股快照聚合计算；候选股得分用于排序，不代表收益概率或买卖建议。"
            "双击候选个股可进入 TradingAgents 四层深度研究。",
        ]
        self.market_text.setPlainText("\n".join(lines))
        self.explanation.setPlainText(result.get("explanation", "无研究解读"))
        self.status_label.setText(
            f"{'美股' if us else 'A股'}扫描完成：{len(stocks)} 只候选个股。"
        )
        self.log_text.append(f"扫描完成。市场={'US' if us else 'A'}，候选 {len(stocks)} 只。")
        self.statusBar().showMessage("扫描完成")

    def _populate_table(self, table, frame, currency="CNY"):
        table.setSortingEnabled(False)
        table.clear()
        if frame is None or frame.empty:
            table.setRowCount(0)
            table.setColumnCount(0)
            table.setSortingEnabled(True)
            return
        preferred = [
            "sector_name", "name", "code", "board_type", "score", "change_pct",
            "breadth", "up_count", "down_count", "amount",
            "market_cap", "float_market_cap", "turnover",
            "volume_ratio", "sector_names", "sector_score", "return_5d_pct",
            "ma5", "ma20", "above_ma5", "above_ma20",
        ]
        cols = [c for c in preferred if c in frame.columns]
        cols += [c for c in frame.columns if c not in cols and c not in ("market", "currency")]
        table.setColumnCount(len(cols))
        table.setHorizontalHeaderLabels(cols)
        table.setRowCount(len(frame))
        for r, (_, row) in enumerate(frame[cols].iterrows()):
            for c, col in enumerate(cols):
                numeric = isinstance(row[col], (int, float)) and pd.notna(row[col])
                if col == "amount" and currency == "USD" and numeric:
                    text = f"{float(row[col]) / 1e8:,.2f} 亿美元"
                elif col in ("market_cap", "float_market_cap") and numeric:
                    unit = "亿美元" if currency == "USD" else "亿元"
                    text = f"{float(row[col]) / 1e8:,.2f} {unit}"
                else:
                    text = display_value(row[col], col)
                item = QTableWidgetItem(text)
                item.setTextAlignment(
                    Qt.AlignVCenter | (Qt.AlignRight if isinstance(row[col], (int, float)) else Qt.AlignLeft)
                )
                if col == "code":
                    item.setData(Qt.UserRole, str(row[col]))
                table.setItem(r, c, item)
        table.setSortingEnabled(True)
        table.resizeColumnsToContents()

    # ---------- 个股深度研究 ----------
    def analyze_selected_stock(self, *args):
        """按钮 / 双击表格 / Ctrl+Enter 共用的深度研究入口。"""
        if self._analyze_state == "busy" or (self.analysis_worker is not None and self.analysis_worker.isRunning()):
            self.statusBar().showMessage("已有研究任务进行中，请等待当前任务完成。", 4000)
            return
        if self._analyze_state != "ready" or self.result is None:
            self.statusBar().showMessage("请先完成市场扫描，并等待候选个股出现。", 4000)
            return
        row_index = self.stock_table.currentRow()
        if row_index < 0:
            if self.stock_table.rowCount() == 0:
                QMessageBox.information(self, "无候选个股", "本次扫描没有可分析的候选个股。")
                return
            # 未选中时默认分析首只，避免弹窗打断操作流
            self.stock_table.selectRow(0)
            row_index = 0
            self.status_label.setText("未选择标的，已默认分析候选个股第一只。")
        code_col = next(
            (
                c
                for c in range(self.stock_table.columnCount())
                if self.stock_table.horizontalHeaderItem(c)
                and self.stock_table.horizontalHeaderItem(c).text() == "code"
            ),
            -1,
        )
        if code_col < 0:
            QMessageBox.warning(self, "无法分析", "候选股表格中没有代码字段。")
            return
        code_item = self.stock_table.item(row_index, code_col)
        code = code_item.data(Qt.UserRole) if code_item else None
        if not code and code_item:
            code = code_item.text()
        stocks = self._candidate_frame()
        matched = stocks[stocks["code"].astype(str) == str(code)]
        if matched.empty:
            QMessageBox.warning(self, "无法分析", f"没有找到 {code} 的扫描记录。")
            return
        stock = matched.iloc[0].to_dict()
        # 二次防护：候选表已在快照阶段按市值过滤，这里再挡一次，确保
        # 「低于门槛就不分析」不会因为结果回填或手工数据而失效。
        cap_filter = self.result.get("market_cap_filter") or {}
        threshold = float(cap_filter.get("threshold", cap_filter.get("threshold_usd", 0)) or 0)
        cap = pd.to_numeric(pd.Series([stock.get("market_cap")]), errors="coerce").iloc[0]
        if threshold > 0 and pd.notna(cap) and float(cap) < threshold:
            cap_unit = "亿美元" if self.is_us() else "亿元"
            QMessageBox.information(
                self,
                "低于市值门槛",
                f"{stock.get('name', code)}（{code}）总市值 "
                f"{float(cap) / 1e8:,.2f} {cap_unit}，低于当前门槛 "
                f"{threshold / 1e8:,.1f} {cap_unit}，已跳过深度研究。",
            )
            return
        sectors = self.result.get("top_sectors", pd.DataFrame())
        if not isinstance(sectors, pd.DataFrame):
            sectors = pd.DataFrame(sectors)
        name = str(stock.get("name", "") or "")
        self._set_analyze_state("busy", code=str(code), name=name)
        self._analysis_started = time.perf_counter()
        self._analysis_tick = 0
        self._analysis_timer.start()
        self.status_label.setText(f"正在对 {name}（{code}）执行 TradingAgents 四层研究……")
        self.statusBar().showMessage(f"正在研究 {name}（{code}）……")
        self.market_combo.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.log_text.append(f"启动四层研究：{name}（{code}）")
        self.analysis_worker = TradingAnalysisWorker(
            stock,
            self.result.get("market_summary", {}),
            sectors,
            market=self.current_market(),
        )
        self.analysis_worker.completed.connect(self.on_stock_analysis_completed)
        self.analysis_worker.failed.connect(self.on_stock_analysis_failed)
        self.analysis_worker.message.connect(self.on_message)
        # 线程真正结束才解锁，避免 completed 之后覆盖仍在收尾的 QThread
        self.analysis_worker.finished.connect(self._on_analysis_finished)
        self.analysis_worker.start()

    def on_stock_analysis_completed(self, result):
        scope = "美股" if self.is_us() else ""
        self.status_label.setText(f"{scope}TradingAgents 四层研究完成。")
        self.log_text.append(
            f"研究完成：{result.get('stock', {}).get('name', '')} " f"{result.get('stock', {}).get('code', '')}"
        )
        # 复用单个结果窗口：先关闭旧窗口再置前，避免重复弹窗互相覆盖或失去焦点
        previous = self.analysis_dialog
        if previous is not None:
            previous.close()
            previous.deleteLater()
        self.analysis_dialog = StockAnalysisDialog(result, self)
        self.analysis_dialog.show()
        self.analysis_dialog.raise_()
        self.analysis_dialog.activateWindow()

    def on_stock_analysis_failed(self, error):
        self.status_label.setText("个股分析失败，请检查行情权限、日线接口与接口配额。")
        self.log_text.append(error)
        QMessageBox.critical(self, "个股分析失败", error[-2500:])

    def on_failed(self, error):
        self.scan_button.setEnabled(True)
        self.market_combo.setEnabled(True)
        self._set_analyze_state("idle")
        self.status_label.setText("扫描失败。请检查 Futu OpenD、账号权限、网络和接口配额。")
        self.log_text.append(error)
        self.statusBar().showMessage("扫描失败")
        QMessageBox.critical(self, "扫描失败", error[-2500:])


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 10))
    window = HunterWindow()
    window.show()

    # 日志同时写入滚动文件并在「运行日志」面板展示；Signal 保证跨线程安全
    bridge = _GuiLogBridge()
    window._log_bridge = bridge  # 保持引用，避免被 GC 回收
    bridge.line.connect(window.append_log_line)
    setup_logging(gui_callback=bridge.line.emit)
    log_path = current_log_file()
    logger.info("GUI 启动完成，日志文件：%s", log_path)
    window.log_text.append(
        f"调试日志已开启：{log_path}；在 .env 设置 LOG_LEVEL=DEBUG 可查看每次行情调用与耗时。"
    )

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
