"""多空研究辩论层：Bull / Bear 研究员陈述与反驳，Research Manager 裁决。

沿用 TradingAgents 的研究团队结构，但论点全部来自本系统分析师 Agent 的
真实结构化输出。规则化生成保证可复现、可审计；不编造任何证据或数据。
"""

from .fundamental_base import score_label

STRENGTH_WEIGHT = {"强": 3, "中": 2, "弱": 1}
SOURCE_ORDER = {"财务": 0, "估值": 1, "新闻": 2, "资金": 3, "情绪": 4, "技术": 5, "风险": 6, "板块": 7}


def _yi(value):
    return "数据不足" if value is None else f"{value / 1e8:.2f}亿元"


def _score_text(value):
    return f"{value:.1f}" if isinstance(value, (int, float)) else "未知"


class ResearchDebateAgent:
    """把分析师报告转化为多空论点、两轮辩论与投资计划。"""

    def run(self, analyst_reports, composite_score=None, coverage=None):
        reports = analyst_reports if isinstance(analyst_reports, dict) else {}
        bull, bear = self._collect_evidence(reports)

        bull.sort(key=lambda e: (-STRENGTH_WEIGHT[e["strength"]], SOURCE_ORDER.get(e["source"], 99)))
        bear.sort(key=lambda e: (-STRENGTH_WEIGHT[e["strength"]], SOURCE_ORDER.get(e["source"], 99)))

        bull_power = sum(STRENGTH_WEIGHT[e["strength"]] for e in bull)
        bear_power = sum(STRENGTH_WEIGHT[e["strength"]] for e in bear)
        verdict, winner = self._verdict(bull_power, bear_power, composite_score)

        rounds = self._build_rounds(bull, bear, composite_score)
        reasoning = (
            f"多头证据 {len(bull)} 条（强度合计 {bull_power}），"
            f"空头证据 {len(bear)} 条（强度合计 {bear_power}）。"
        )
        if composite_score is not None:
            reasoning += f"综合评分 {composite_score:.1f}/100"
            if coverage is not None:
                reasoning += f"（覆盖 {coverage:.0%}）"
            reasoning += " 作为交叉验证。"
        reasoning += f" 研究主管裁决：{verdict}。"

        plan = self._build_plan(bull, bear, reports, verdict)

        return {
            "agent": "研究辩论 Agent",
            "status": "完成" if (bull or bear) else "数据不足",
            "bull_case": {"arguments": bull, "power": bull_power,
                          "summary": self._summary("多头", bull)},
            "bear_case": {"arguments": bear, "power": bear_power,
                          "summary": self._summary("空头", bear)},
            "rounds": rounds,
            "judge": {"verdict": verdict, "winner": winner, "reasoning": reasoning},
            "investment_plan": plan,
            "conclusion": (
                f"多空辩论裁决：{verdict}。{reasoning}"
                f" 投资计划：{plan['thesis']} 证据均来自分析师 Agent 的结构化输出，"
                "辩论结论不构成买卖指令。"
            ),
        }

    # ------------------------------------------------------------------
    # 证据提取
    # ------------------------------------------------------------------
    @classmethod
    def _collect_evidence(cls, reports):
        bull, bear = [], []

        def add(target, source, text, strength):
            target.append({"source": source, "text": text, "strength": strength})

        # 财务
        fin = reports.get("财务 Agent") or {}
        score = fin.get("score")
        if score is not None:
            if score >= 65:
                add(bull, "财务", f"财务维度评分 {score:.1f}，质量偏强", cls._grade(score))
            elif score <= 45:
                add(bear, "财务", f"财务维度评分 {score:.1f}，质量偏弱", cls._grade(100 - score))
        roe = fin.get("roe_pct")
        if roe is not None:
            if roe >= 15:
                add(bull, "财务", f"ROE {roe:.1f}%，高于 15% 的较优线", "中")
            elif roe < 8:
                add(bear, "财务", f"ROE {roe:.1f}%，低于 8% 的参考线", "中")
        for key, label in (("revenue_yoy_pct", "营收同比"), ("net_profit_yoy_pct", "净利润同比")):
            value = fin.get(key)
            if value is None:
                continue
            if value >= 15:
                add(bull, "财务", f"{label} {value:.1f}%，增长动能较强", "中")
            elif value <= 0:
                add(bear, "财务", f"{label} {value:.1f}%，盈利增长承压", "中" if value > -20 else "强")

        # 估值
        val = reports.get("估值 Agent") or {}
        pct = val.get("pe_percentile")
        if pct is not None:
            if pct <= 30:
                add(bull, "估值", f"PE 历史分位 {pct:.1f}%，处于历史低位", "强" if pct <= 15 else "中")
            elif pct >= 70:
                add(bear, "估值", f"PE 历史分位 {pct:.1f}%，存在估值回归压力", "强" if pct >= 85 else "中")
        peg = val.get("peg")
        if peg is not None:
            if peg < 1:
                add(bull, "估值", f"PEG {peg:.2f}，估值与增速匹配度较好", "中")
            elif peg > 2:
                add(bear, "估值", f"PEG {peg:.2f}，估值相对增速偏高", "中")

        # 新闻
        news = reports.get("新闻 Agent") or {}
        n_bull = news.get("bullish_count") or 0
        n_bear = news.get("bearish_count") or 0
        if n_bull > n_bear:
            add(bull, "新闻", f"相关线索正面 {n_bull} 条 / 负面 {n_bear} 条", "中")
        elif n_bear > n_bull:
            add(bear, "新闻", f"相关线索负面 {n_bear} 条 / 正面 {n_bull} 条", "中")
        tags = {t for item in (news.get("items") or []) for t in (item.get("tags") or [])}
        if "风险事项" in tags:
            add(bear, "新闻", "公告标题中出现风险事项标签，需核实原文", "中")

        # 资金
        cap = reports.get("资金 Agent") or {}
        flow = cap.get("main_net_inflow_latest")
        if flow is not None:
            direction = "净流入" if flow > 0 else ("净流出" if flow < 0 else "持平")
            target = bull if flow > 0 else (bear if flow < 0 else bull)
            add(target, "资金", f"最近一日主力{direction} {_yi(abs(flow))}", "中")
        inst = cap.get("institutional_holder_count")
        if inst:
            add(bull, "资金", f"机构持股记录 {inst} 条（需以定期报告核验）", "弱")

        # 情绪
        sent = reports.get("情绪面 Agent") or {}
        if sent.get("sentiment") == "偏乐观":
            add(bull, "情绪", f"市场情绪偏乐观（{_score_text(sent.get('score'))}）", "中")
        elif sent.get("sentiment") == "偏悲观":
            add(bear, "情绪", f"市场情绪偏悲观（{_score_text(sent.get('score'))}）", "中")

        # 技术面
        trend = (reports.get("趋势 Agent") or {}).get("trend")
        if trend == "多头排列":
            add(bull, "技术", "均线呈多头排列，短中期趋势偏强", "强")
        elif trend == "空头排列":
            add(bear, "技术", "均线呈空头排列，短中期趋势偏弱", "强")
        momentum = (reports.get("动量 Agent") or {}).get("momentum")
        if momentum == "动量偏强":
            add(bull, "技术", "动量指标偏强", "中")
        elif momentum == "动量偏弱":
            add(bear, "技术", "动量指标偏弱", "中")
        elif momentum == "短线偏热":
            add(bear, "技术", "RSI 偏高，存在追高回撤风险", "弱")

        # 风险 Agent（过滤占位提示）
        for flag in (reports.get("风险 Agent") or {}).get("risk_flags") or []:
            if "未触发" in flag or "不代表" in flag or "数据不足" in flag:
                continue
            add(bear, "风险", flag, "中" if "回撤超过" in flag else "弱")

        # 板块
        sectors = (reports.get("板块 Agent") or {}).get("matched_sectors") or []
        if sectors:
            names = "、".join(x.get("sector_name", "") for x in sectors[:3])
            add(bull, "板块", f"匹配到所属强势板块：{names}", "弱")

        return bull, bear

    @staticmethod
    def _grade(score):
        if score >= 80:
            return "强"
        if score >= 65:
            return "中"
        return "弱"

    # ------------------------------------------------------------------
    # 辩论与裁决
    # ------------------------------------------------------------------
    @staticmethod
    def _verdict(bull_power, bear_power, composite_score):
        if bull_power == 0 and bear_power == 0:
            return "证据不足", "无"
        if bull_power >= bear_power * 1.4 and bull_power >= 4:
            verdict, winner = "偏多", "多头研究员"
        elif bear_power >= bull_power * 1.4 and bear_power >= 4:
            verdict, winner = "偏空", "空头研究员"
        else:
            verdict, winner = "中性", "双方均势"
        # 综合评分仅作为均势时的交叉验证，不单独制造方向
        if verdict == "中性" and composite_score is not None:
            if composite_score >= 70:
                verdict = "偏多（弱）"
            elif composite_score <= 35:
                verdict = "偏空（弱）"
        return verdict, winner

    @staticmethod
    def _summary(side, arguments):
        if not arguments:
            return f"{side}暂无可核验证据。"
        top = "；".join(e["text"] for e in arguments[:3])
        return f"{side}共 {len(arguments)} 条证据，核心论点：{top}。"

    @staticmethod
    def _rebuttal(own, opponent, composite_score):
        if not opponent:
            return "对方未提出可核验的反向证据，本方论点未被削弱。"
        target = opponent[0]["text"]
        if own:
            return f"针对『{target}』：{own[0]['text']}，该反向信号不足以推翻本方判断。"
        score_text = f"综合评分 {composite_score:.1f}" if composite_score is not None else "现有数据"
        return f"针对『{target}』：本方缺少可核验的对应证据，需以{score_text}及后续数据再确认。"

    @classmethod
    def _build_rounds(cls, bull, bear, composite_score):
        if not bull and not bear:
            return []
        return [
            {
                "round": 1,
                "bull": {"statement": cls._summary("多头", bull),
                         "arguments": [e["text"] for e in bull[:3]]},
                "bear": {"statement": cls._summary("空头", bear),
                         "arguments": [e["text"] for e in bear[:3]]},
            },
            {
                "round": 2,
                "bull_rebuttal": cls._rebuttal(bull, bear, composite_score),
                "bear_rebuttal": cls._rebuttal(bear, bull, composite_score),
            },
        ]

    @staticmethod
    def _build_plan(bull, bear, reports, verdict):
        if verdict.startswith("偏多"):
            thesis = "多头证据占优，可将其作为方向性研究假设，等待入场条件确认。"
        elif verdict.startswith("偏空"):
            thesis = "空头证据占优，应以风险规避或减仓思路为主，避免追高。"
        else:
            thesis = "多空证据均势，暂不形成方向性假设，等待关键数据确认。"

        has_fundamental = bool(reports.get("财务 Agent") or reports.get("估值 Agent"))
        horizon = "中线（1-3个月）" if has_fundamental else "短线（1-4周）"
        return {
            "thesis": thesis,
            "verdict": verdict,
            "entry_conditions": [e["text"] for e in bull[:3]] or ["等待更多可核验数据"],
            "exit_conditions": [e["text"] for e in bear[:3]] or ["按预设风险阈值退出"],
            "time_horizon": horizon,
        }
