import sys
import traceback
from pathlib import Path

import pandas as pd
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit,
    QFrame, QMessageBox, QHeaderView, QStatusBar, QDialog, QProgressDialog
)

from ashare_hunter.graph.workflow import build_workflow
from ashare_hunter.agents.stock_analysis import StockAnalysisOrchestrator
from ashare_hunter.agents.trading_agents_orchestrator import TradingAgentsOrchestrator
from ashare_hunter.data.futu_provider import FutuProvider
from config import settings


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


class ScanWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    message = Signal(str)

    def run(self):
        try:
            self.message.emit("正在连接 Futu OpenD 并获取行情……")
            result = build_workflow().invoke({})
            self.completed.emit(result)
        except Exception:
            self.failed.emit(traceback.format_exc())


class StockAnalysisWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    message = Signal(str)

    def __init__(self, stock, market, sectors):
        super().__init__()
        self.stock = stock
        self.market = market
        self.sectors = sectors

    def run(self):
        provider = None
        try:
            self.message.emit(f"正在对 {self.stock.get('name', '')} ({self.stock.get('code', '')}) 执行 TradingAgents 四层研究……")
            provider = FutuProvider(
                host=settings.futu_host,
                port=settings.futu_port,
                max_plate_refresh_per_run=settings.futu_max_plate_refresh_per_run,
                plate_cache_days=settings.futu_plate_cache_days,
                request_interval=settings.futu_request_interval,
            )
            result = TradingAgentsOrchestrator(provider).analyze(
                self.stock, self.market, self.sectors
            )
            self.completed.emit(result)
        except Exception:
            self.failed.emit(traceback.format_exc())
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

    def set_value(self, value, hint=""):
        self.value.setText(str(value))
        self.hint.setText(hint)


class HunterWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("A-Share Hunter · A股强势板块与个股分析")
        self.resize(1420, 900)
        self.worker = None
        self.result = None
        self._build_ui()
        self._apply_style()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("A-Share Hunter")
        title.setObjectName("mainTitle")
        subtitle = QLabel("基于 Futu OpenD + LangGraph 的 A股强势板块 / 强势个股发现")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.scan_button = QPushButton("▶  开始扫描")
        self.scan_button.setObjectName("scanButton")
        self.scan_button.clicked.connect(self.start_scan)
        header.addWidget(self.scan_button)
        self.analyze_button = QPushButton("◎  TradingAgents 深度研究")
        self.analyze_button.setEnabled(False)
        self.analyze_button.clicked.connect(self.analyze_selected_stock)
        header.addWidget(self.analyze_button)
        root.addLayout(header)

        self.status_label = QLabel("尚未扫描。请先启动 Futu OpenD 并登录具备 A股行情权限的账号。")
        self.status_label.setObjectName("statusLabel")
        root.addWidget(self.status_label)

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

        self.stock_table = self._make_table()
        self.stock_table.cellDoubleClicked.connect(self.analyze_selected_stock)
        self.tabs.addTab(self.stock_table, "候选个股")

        self.explanation = QTextEdit()
        self.explanation.setReadOnly(True)
        self.tabs.addTab(self.explanation, "Agent 研究解读")

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
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
            QPushButton#scanButton { background: #2457d6; color: white; border: 0; border-radius: 8px; padding: 11px 20px; font-weight: 600; }
            QPushButton#scanButton:hover { background: #1948bd; }
            QPushButton#scanButton:disabled { background: #9aabd2; }
            QTabWidget::pane { border: 1px solid #e3e8f0; background: white; border-radius: 8px; }
            QTabBar::tab { background: #e9eef6; padding: 10px 18px; margin-right: 4px; border-top-left-radius: 7px; border-top-right-radius: 7px; }
            QTabBar::tab:selected { background: white; color: #2457d6; font-weight: 600; }
            QTableWidget, QTextEdit { background: white; border: 1px solid #e4e9f1; border-radius: 7px; gridline-color: #edf0f5; }
            QHeaderView::section { background: #f2f5fa; color: #475467; border: 0; padding: 8px; font-weight: 600; }
        """)

    def start_scan(self):
        self.scan_button.setEnabled(False)
        self.status_label.setText("扫描运行中：正在连接行情服务、计算板块与个股强度……")
        self.log_text.append("开始执行 LangGraph 扫描工作流。")
        self.statusBar().showMessage("扫描中……")
        self.worker = ScanWorker()
        self.worker.message.connect(self.on_message)
        self.worker.completed.connect(self.on_completed)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def on_message(self, message):
        self.status_label.setText(message)
        self.log_text.append(message)

    def on_completed(self, result):
        self.result = result
        self.scan_button.setEnabled(True)
        market = result.get("market_summary", {})
        sectors = result.get("top_sectors", pd.DataFrame())
        stocks = result.get("top_stocks", pd.DataFrame())
        if not isinstance(sectors, pd.DataFrame):
            sectors = pd.DataFrame(sectors)
        if not isinstance(stocks, pd.DataFrame):
            stocks = pd.DataFrame(stocks)

        self.card_regime.set_value(market.get("market_regime", "未知"), "市场宽度与平均涨跌幅综合判断")
        breadth = market.get("advance_decline_ratio")
        self.card_breadth.set_value(f"{breadth:.1%}" if isinstance(breadth, (float, int)) else "—",
                                    f"上涨 {market.get('up_count', 0)} / 下跌 {market.get('down_count', 0)}")
        self.card_avg.set_value(f"{market.get('avg_change_pct', 0):.2f}%", "全市场快照平均涨跌幅")
        amount = market.get("total_amount_yuan", 0)
        self.card_amount.set_value(f"{amount / 1e8:.2f} 亿", "快照成交额汇总（需核验数据字段口径）")
        self.card_sector.set_value(len(sectors), "量化评分靠前的正涨幅板块")
        self.card_stock.set_value(len(stocks), "通过过滤并完成综合排序的候选股")

        self._populate_table(self.sector_table, sectors)
        self._populate_table(self.stock_table, stocks)
        self.analyze_button.setEnabled(not stocks.empty)

        lines = [
            f"扫描时间：{result.get('scan_time', '未知')}",
            f"市场环境：{market.get('market_regime', '未知')}",
            f"上涨 / 下跌 / 平盘：{market.get('up_count', 0)} / {market.get('down_count', 0)} / {market.get('flat_count', 0)}",
            f"上涨家数占比：{market.get('advance_decline_ratio', 0):.1%}",
            f"平均涨跌幅：{market.get('avg_change_pct', 0):.3f}%",
            f"报告文件：{result.get('report_path', '未生成')}",
            "",
            "说明：板块强度由成分股聚合计算，不等同于官方板块指数涨幅。候选股得分用于排序，不代表收益概率或买卖建议。"
        ]
        self.market_text.setPlainText("\\n".join(lines))
        self.explanation.setPlainText(result.get("explanation", "无研究解读"))
        self.status_label.setText(f"扫描完成：{len(sectors)} 个强势板块，{len(stocks)} 只候选个股。")
        self.log_text.append(f"扫描完成。报告：{result.get('report_path', '无')}")
        self.statusBar().showMessage("扫描完成")

    def _populate_table(self, table, frame):
        table.setSortingEnabled(False)
        table.clear()
        if frame is None or frame.empty:
            table.setRowCount(0)
            table.setColumnCount(0)
            table.setSortingEnabled(True)
            return
        # Prefer useful columns first, retain remaining fields after them.
        preferred = [
            "sector_name", "name", "code", "board_type", "score", "change_pct",
            "breadth", "up_count", "down_count", "amount", "turnover",
            "volume_ratio", "sector_names", "sector_score", "return_5d_pct",
            "ma5", "ma20", "above_ma5", "above_ma20"
        ]
        cols = [c for c in preferred if c in frame.columns]
        cols += [c for c in frame.columns if c not in cols]
        table.setColumnCount(len(cols))
        table.setHorizontalHeaderLabels(cols)
        table.setRowCount(len(frame))
        for r, (_, row) in enumerate(frame[cols].iterrows()):
            for c, col in enumerate(cols):
                item = QTableWidgetItem(display_value(row[col], col))
                item.setTextAlignment(Qt.AlignVCenter | (Qt.AlignRight if isinstance(row[col], (int, float)) else Qt.AlignLeft))
                if col == "code":
                    item.setData(Qt.UserRole, str(row[col]))
                table.setItem(r, c, item)
        table.setSortingEnabled(True)
        table.resizeColumnsToContents()


    def analyze_selected_stock(self, *args):
        row_index = self.stock_table.currentRow()
        if row_index < 0:
            QMessageBox.information(self, "请选择股票", "请先在候选个股表格中选择一只股票。")
            return
        code_col = -1
        for col in range(self.stock_table.columnCount()):
            header = self.stock_table.horizontalHeaderItem(col)
            if header and header.text() == "code":
                code_col = col
                break
        if code_col < 0:
            QMessageBox.warning(self, "无法分析", "候选股表格中没有股票代码字段。")
            return
        code_item = self.stock_table.item(row_index, code_col)
        code = code_item.data(Qt.UserRole) if code_item else None
        if not code:
            code = code_item.text() if code_item else ""
        stocks = self.result.get("top_stocks", pd.DataFrame())
        if not isinstance(stocks, pd.DataFrame):
            stocks = pd.DataFrame(stocks)
        matched = stocks[stocks["code"].astype(str) == str(code)]
        if matched.empty:
            QMessageBox.warning(self, "无法分析", f"没有找到股票 {code} 的扫描记录。")
            return
        stock = matched.iloc[0].to_dict()
        sectors = self.result.get("top_sectors", pd.DataFrame())
        if not isinstance(sectors, pd.DataFrame):
            sectors = pd.DataFrame(sectors)
        self.analyze_button.setEnabled(False)
        self.status_label.setText(f"正在对 {stock.get('name', '')}（{code}）执行 TradingAgents 四层研究……")
        self.analysis_worker = StockAnalysisWorker(
            stock, self.result.get("market_summary", {}), sectors
        )
        self.analysis_worker.completed.connect(self.on_stock_analysis_completed)
        self.analysis_worker.failed.connect(self.on_stock_analysis_failed)
        self.analysis_worker.message.connect(self.on_message)
        self.analysis_worker.start()

    def on_stock_analysis_completed(self, result):
        self.analyze_button.setEnabled(True)
        self.status_label.setText("TradingAgents 四层研究完成。")
        self.log_text.append(
            f"TradingAgents 研究完成：{result.get('stock', {}).get('name', '')} "
            f"{result.get('stock', {}).get('code', '')}"
        )
        self.analysis_dialog = StockAnalysisDialog(result, self)
        self.analysis_dialog.show()

    def on_stock_analysis_failed(self, error):
        self.analyze_button.setEnabled(True)
        self.status_label.setText("个股多Agent分析失败，请检查行情权限和日线接口。")
        self.log_text.append(error)
        QMessageBox.critical(self, "个股分析失败", error[-2500:])

    def on_failed(self, error):
        self.scan_button.setEnabled(True)
        self.status_label.setText("扫描失败。请检查 Futu OpenD、账号权限、网络和接口配额。")
        self.log_text.append(error)
        self.statusBar().showMessage("扫描失败")
        QMessageBox.critical(self, "扫描失败", error[-2500:])


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 10))
    window = HunterWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
