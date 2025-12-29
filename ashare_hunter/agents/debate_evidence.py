"""多空辩论的四层证据构建器。

把「基本面 / 技术面 / 新闻面 / 宏观面」的检查问题转成可核验的结构化证据：
每条证据都带判定、数值依据、数据来源、数据时间和证据出处，并标注归入多头
还是空头。核心原则与项目一致：

1. 只消费分析师 Agent 的真实结构化输出与 Futu 返回的字段，不新增事实。
2. 数据源缺失一律标记「数据不足」，不用 0、不猜测、不把「未检索到」写成「不存在」。
3. 宏观层（利率、产业景气度）目前没有自动数据源，默认数据不足；仅当调用方
   显式注入带来源的 macro 上下文时才生成证据，并标注为外部录入。
4. 板块行情只能作为产业景气度的弱代理，文案必须写明口径差异。
"""

from .fundamental_base import to_number

LAYERS = ("基本面", "技术面", "新闻面", "宏观面")
LAYER_SOURCE = {"基本面": "财务", "技术面": "技术", "新闻面": "新闻", "宏观面": "宏观"}

# 用于从强势板块中识别科技 / AI 相关板块（仅作行情代理，不代表产业景气度）
TECH_KEYWORDS = (
    "ai", "人工智能", "算力", "半导体", "芯片", "科技", "软件", "云计算",
    "数据中心", "光模块", "电子", "计算机", "semiconductor", "software",
)

# 外部录入的宏观判定词 -> 多空方向
MACRO_JUDGEMENT = {
    "有利": "bull", "改善": "bull", "提升": "bull", "上行": "bull", "宽松": "bull",
    "不利": "bear", "恶化": "bear", "回落": "bear", "下行": "bear", "收紧": "bear",
    "中性": "neutral", "持平": "neutral",
}


def _rows(sectors):
    """把板块数据统一成 dict 列表，兼容 DataFrame / list / None。"""
    if sectors is None:
        return []
    if hasattr(sectors, "to_dict"):
        try:
            return [r for r in sectors.to_dict("records") if isinstance(r, dict)]
        except Exception:
            return []
    if isinstance(sectors, (list, tuple)):
        return [r for r in sectors if isinstance(r, dict)]
    return []


def _sample_text(samples):
    """取一条代表性线索，含标题与时间，便于回溯原文。"""
    if not samples:
        return ""
    item = samples[0]
    title = str(item.get("title") or "").strip()
    date = str(item.get("publish_time") or "").strip()
    source = str(item.get("source") or "").strip()
    parts = [p for p in (title, date, source) if p]
    return "《" + "｜".join(parts) + "》" if parts else ""


def _make(item_id, layer, question, answer, detail, source, as_of=None,
          ref=None, value=None, direction="unknown", strength=None):
    return {
        "id": item_id, "layer": layer, "question": question,
        "answer": answer, "value": value, "detail": detail,
        "source": source, "as_of": as_of or "未提供", "ref": ref or source,
        "direction": direction, "strength": strength,
    }


def _missing(item_id, layer, question, source, reason):
    return _make(item_id, layer, question, "数据不足", reason, source)


def _status_direction(status, bull_strength, bear_strength):
    """把「是 / 否 / 部分 / 数据不足」映射为多空方向与强度。"""
    if status == "是":
        return "bull", bull_strength
    if status == "否":
        return "bear", bear_strength
    if status == "部分":
        return "neutral", "弱"
    return "unknown", None


class DebateEvidenceBuilder:
    """消费分析师报告与扫描上下文，生成四层证据矩阵与多空论点。"""

    def build(self, reports, context=None):
        ctx = context or {}
        reports = reports if isinstance(reports, dict) else {}
        items = []
        items += self._fundamental(reports.get("财务 Agent") or {})
        items += self._technical(reports.get("趋势 Agent") or {},
                                 reports.get("动量 Agent") or {})
        items += self._news(reports.get("新闻 Agent") or {},
                            reports.get("资金 Agent") or {})
        items += self._macro(ctx)

        by_layer = {layer: [x for x in items if x["layer"] == layer] for layer in LAYERS}
        coverage = {
            "total": len(items),
            "answered": sum(1 for x in items if x["answer"] != "数据不足"),
            "by_layer": {},
        }
        for layer, rows in by_layer.items():
            coverage["by_layer"][layer] = {
                "total": len(rows),
                "answered": sum(1 for x in rows if x["answer"] != "数据不足"),
                "bull": sum(1 for x in rows if x["direction"] == "bull"),
                "bear": sum(1 for x in rows if x["direction"] == "bear"),
                "missing": sum(1 for x in rows if x["answer"] == "数据不足"),
            }
        arguments = [self._argument(x) for x in items
                     if x["direction"] in ("bull", "bear") and x["strength"]]
        unresolved = [x["question"] for x in items if x["answer"] == "数据不足"]
        return {
            "items": items,
            "by_layer": by_layer,
            "coverage": coverage,
            "arguments": arguments,
            "unresolved": unresolved,
        }

    @staticmethod
    def _argument(item):
        return {
            "source": LAYER_SOURCE.get(item["layer"], "其他"),
            "layer": item["layer"],
            "question": item["question"],
            "answer": item["answer"],
            "text": f"【{item['layer']}·{item['question']}】{item['detail']}"
                    f"（{item['source']}，{item['as_of']}）",
            "strength": item["strength"],
            "direction": item["direction"],
        }

    # ------------------------------------------------------------------
    # 第一层：基本面
    # ------------------------------------------------------------------
    def _fundamental(self, fin):
        trend = fin.get("growth_trend") or {}
        source = "Futu 财报接口（年报多期）"
        items = []

        growth_questions = (
            ("F1", "营收是否持续增长？", "revenue", "营业收入", fin.get("revenue_yoy_pct")),
            ("F2", "净利润是否增长？", "net_profit", "归母净利润", fin.get("net_profit_yoy_pct")),
        )
        for item_id, question, key, label, yoy in growth_questions:
            block = trend.get(key) or {}
            status = block.get("status", "数据不足")
            if status == "数据不足":
                # 单期同比只能回答「是否增长」，回答不了「是否持续增长」
                if to_number(yoy) is not None:
                    dim = (fin.get("dimensions") or {}).get(key) or {}
                    items.append(_make(
                        item_id, "基本面", question, "部分",
                        f"仅取得单期同比 {float(yoy):+.2f}%（{label}），未取得多期序列，"
                        "无法判断是否持续增长",
                        source, as_of=dim.get("period"),
                        ref=f"{label} 单期同比", value=float(yoy),
                        direction="unknown", strength=None,
                    ))
                else:
                    items.append(_missing(
                        item_id, "基本面", question, source,
                        "未取得可核验的多期财报序列，也不含单期同比，增长趋势不作判断",
                    ))
                continue
            direction, strength = _status_direction(status, "强", "中")
            latest = block.get("latest_change_pct")
            latest_text = f"{latest:+.2f}%" if isinstance(latest, (int, float)) else "未提供"
            detail = f"{block.get('detail')}；最近一期同比 {latest_text}"
            items.append(_make(
                item_id, "基本面", question, status, detail, source,
                as_of=block.get("as_of"), ref=block.get("source_field") or label,
                value=block.get("latest_change_pct"),
                direction=direction, strength=strength,
            ))

        for item_id, question, key, label in (
            ("F3", "毛利率是否改善？", "gross_margin", "销售毛利率"),
            ("F4", "ROE 是否提高？", "roe", "净资产收益率"),
        ):
            block = trend.get(key) or {}
            status = block.get("status", "数据不足")
            if status == "数据不足":
                items.append(_missing(
                    item_id, "基本面", question, source,
                    f"未取得 {label} 的两期以上可核验数值，无法判断改善或恶化",
                ))
                continue
            direction, strength = _status_direction(status, "中", "中")
            items.append(_make(
                item_id, "基本面", question, status,
                f"{label}{block.get('direction')}：{block.get('detail')}",
                source, as_of=block.get("as_of"), ref=label,
                value=block.get("delta_pp"),
                direction=direction, strength=strength,
            ))
        return items

    # ------------------------------------------------------------------
    # 第二层：技术面
    # ------------------------------------------------------------------
    def _technical(self, tr, mom):
        source = "Futu 历史 K 线（前复权日线）"
        as_of = tr.get("as_of") or mom.get("as_of")
        items = []

        breakout = tr.get("ma20_breakout")
        breakdown = tr.get("ma20_breakdown")
        if breakout is True:
            items.append(_make(
                "T1", "技术面", "股价是否突破关键均线？", "是",
                f"最新收盘 {tr.get('last_close')} 上穿 MA20（{tr.get('ma20')}），"
                "前一日收盘位于 MA20 之下",
                source, as_of=as_of, ref="收盘价 / MA20 上穿事件",
                value=tr.get("last_close"), direction="bull", strength="中",
            ))
        elif breakdown is True:
            items.append(_make(
                "T1", "技术面", "股价是否突破关键均线？", "否",
                f"最新收盘 {tr.get('last_close')} 跌破 MA20（{tr.get('ma20')}），"
                "前一日收盘位于 MA20 之上",
                source, as_of=as_of, ref="收盘价 / MA20 下穿事件",
                value=tr.get("last_close"), direction="bear", strength="中",
            ))
        elif tr.get("above_ma20") is not None:
            items.append(_make(
                "T1", "技术面", "股价是否突破关键均线？", "部分",
                f"收盘价{'位于' if tr.get('above_ma20') else '低于'} MA20，"
                "但当日未发生上穿/下穿事件",
                source, as_of=as_of, ref="收盘价 / MA20 位置",
                value=tr.get("ma20"), direction="neutral", strength="弱",
            ))
        else:
            items.append(_missing(
                "T1", "技术面", "股价是否突破关键均线？", source,
                "趋势 Agent 未返回 MA20 突破字段，需至少 21 根有效日线",
            ))

        golden = mom.get("macd_golden_cross")
        dead = mom.get("macd_dead_cross")
        if golden is None or dead is None:
            items.append(_missing(
                "T2", "技术面", "MACD 是否形成金叉？", source,
                "有效日线不足 35 根或动量 Agent 未返回穿越判定，不制造 MACD 信号",
            ))
        elif golden and not dead:
            items.append(_make(
                "T2", "技术面", "MACD 是否形成金叉？", "是",
                f"近 {mom.get('macd_cross_window') or 5} 根日线内 DIF 上穿 DEA"
                f"（最近穿越日 {mom.get('macd_cross_date') or '未提供'}），MACD 柱 {mom.get('macd_hist')}",
                source, as_of=as_of, ref="MACD DIF / DEA 金叉",
                value=mom.get("macd_hist"), direction="bull", strength="中",
            ))
        elif dead and not golden:
            items.append(_make(
                "T2", "技术面", "MACD 是否形成金叉？", "否",
                f"近 {mom.get('macd_cross_window') or 5} 根日线内出现 DIF 下穿 DEA 死叉"
                f"（{mom.get('macd_cross_date') or '日期未提供'}）",
                source, as_of=as_of, ref="MACD DIF / DEA 死叉",
                value=mom.get("macd_hist"), direction="bear", strength="中",
            ))
        else:
            items.append(_make(
                "T2", "技术面", "MACD 是否形成金叉？", "否",
                "观察窗口内未出现金叉；若为短窗口内先死叉后金叉，信号需人工复核",
                source, as_of=as_of, ref="MACD 穿越序列",
                value=mom.get("macd_hist"), direction="bear", strength="弱",
            ))

        vol_status = mom.get("volume_status") or "数据不足"
        ratio = mom.get("volume_expansion")
        if vol_status == "数据不足":
            items.append(_missing(
                "T3", "技术面", "成交量是否放大？", source,
                "日线未返回成交量字段或有效样本不足 25 根，量能不作判断",
            ))
        else:
            direction = {"放大": "bull", "萎缩": "bear", "持平": "neutral"}.get(vol_status, "unknown")
            items.append(_make(
                "T3", "技术面", "成交量是否放大？",
                "是" if vol_status == "放大" else ("否" if vol_status == "萎缩" else "部分"),
                f"近 5 日均量 / 之前 20 日均量 = {ratio}（{vol_status}）；"
                "阈值：≥1.2 放大，≤0.8 萎缩",
                source, as_of=as_of, ref="日线 volume 均量比",
                value=ratio, direction=direction,
                strength="中" if vol_status == "放大" else "弱",
            ))

        trend_label = tr.get("trend")
        uptrend = tr.get("uptrend")
        if uptrend is None and trend_label:
            uptrend = trend_label == "多头排列"
        if uptrend is True:
            items.append(_make(
                "T4", "技术面", "是否形成上升趋势？", "是",
                f"均线多头排列（收盘 > MA5 > MA10 > MA20）；趋势判定：{trend_label or '多头排列'}",
                source, as_of=as_of, ref="趋势 Agent · 多头排列",
                direction="bull", strength="强",
            ))
        elif trend_label == "空头排列":
            items.append(_make(
                "T4", "技术面", "是否形成上升趋势？", "否",
                "均线空头排列（收盘 < MA5 < MA10 < MA20）",
                source, as_of=as_of, ref="趋势 Agent · 空头排列",
                direction="bear", strength="中",
            ))
        elif trend_label:
            items.append(_make(
                "T4", "技术面", "是否形成上升趋势？", "部分",
                f"趋势判定为「{trend_label}」，尚未形成完整多头排列",
                source, as_of=as_of, ref="趋势 Agent · 均线形态",
                direction="neutral", strength="弱",
            ))
        else:
            items.append(_missing(
                "T4", "技术面", "是否形成上升趋势？", source,
                "趋势 Agent 未返回有效日线趋势判定",
            ))
        return items

    # ------------------------------------------------------------------
    # 第三层：新闻面
    # ------------------------------------------------------------------
    def _news(self, news, capital):
        evidence = news.get("event_evidence")
        if not isinstance(evidence, dict):
            # 兼容旧结构：从 items 的 tags 现场聚合
            evidence = {}
            for item in news.get("items") or []:
                for tag in item.get("tags") or []:
                    bucket = evidence.setdefault(tag, {"count": 0, "samples": []})
                    bucket["count"] += 1
                    if len(bucket["samples"]) < 3:
                        bucket["samples"].append({
                            "title": item.get("title"), "publish_time": item.get("publish_time"),
                            "source": item.get("source"),
                        })
        has_data = bool(news.get("items")) or bool(capital.get("analyst_rating_count"))
        source = "Futu 资讯 / 公告 / 评级检索（标题关键词标记）"
        items = []

        def event_question(item_id, question, tag, opposite_tag=None):
            bucket = evidence.get(tag) or {}
            count = int(bucket.get("count") or 0)
            if count:
                sample = _sample_text(bucket.get("samples"))
                items.append(_make(
                    item_id, "新闻面", question, "是",
                    f"检索到 {count} 条「{tag}」线索 {sample}；"
                    "事件标签由标题关键词生成，需以公告/研报原文核实",
                    source, as_of=(bucket.get("samples") or [{}])[0].get("publish_time"),
                    ref=tag, value=count, direction="bull", strength="中",
                ))
                return
            opposite = evidence.get(opposite_tag) or {} if opposite_tag else {}
            if int(opposite.get("count") or 0):
                sample = _sample_text(opposite.get("samples"))
                items.append(_make(
                    item_id, "新闻面", question, "否",
                    f"未检索到「{tag}」，但出现 {opposite.get('count')} 条「{opposite_tag}」线索 {sample}",
                    source, as_of=(opposite.get("samples") or [{}])[0].get("publish_time"),
                    ref=opposite_tag, direction="bear", strength="中",
                ))
                return
            if not has_data:
                items.append(_missing(
                    item_id, "新闻面", question, source,
                    "资讯接口未返回相关线索；未返回不等于事件不存在",
                ))
            else:
                items.append(_make(
                    item_id, "新闻面", question, "否",
                    f"已取得 {len(news.get('items') or [])} 条相关线索，但未命中「{tag}」关键词；"
                    "未检索到不等于不存在",
                    source, ref=tag, direction="bear", strength="弱",
                ))

        event_question("N1", "是否有新产品发布？", "新产品/新技术")
        event_question("N2", "是否有重大订单？", "重大合同/订单")
        event_question("N3", "是否有行业利好？", "行业政策/景气")
        event_question("N4", "是否有机构上调评级？", "机构评级上调", "机构评级下调")

        # 评级概述只有条数、没有方向字段时，不算「已上调」的证据
        rating_item = items[-1]
        if (rating_item["answer"] == "否" and rating_item["strength"] == "弱"
                and capital.get("analyst_rating_count")):
            rating_item["answer"] = "数据不足"
            rating_item["detail"] = (
                f"Futu 分析师评级概述返回 {capital['analyst_rating_count']} 条记录，"
                "但未提供可核验的评级方向字段，无法判断是否上调"
            )
            rating_item["direction"] = "unknown"
            rating_item["strength"] = None
            rating_item["source"] = "Futu get_research_analyst_consensus（评级为观点，不等同于持仓）"
        return items

    # ------------------------------------------------------------------
    # 第四层：宏观面（无自动数据源，默认数据不足）
    # ------------------------------------------------------------------
    def _macro(self, ctx):
        macro = ctx.get("macro") if isinstance(ctx.get("macro"), dict) else {}
        market = ctx.get("market") if isinstance(ctx.get("market"), dict) else {}
        items = []

        rate = macro.get("rate") if isinstance(macro.get("rate"), dict) else {}
        judgement = str(rate.get("judgement") or "").strip()
        rate_source = str(rate.get("source") or "").strip()
        if judgement in MACRO_JUDGEMENT and rate_source:
            direction = MACRO_JUDGEMENT[judgement]
            items.append(_make(
                "M1", "宏观面", "利率是否有利于科技股？",
                "是" if direction == "bull" else ("否" if direction == "bear" else "部分"),
                f"外部录入判定：{judgement}"
                + (f"；依据 {rate.get('value')}" if rate.get("value") else "")
                + (f"；备注 {rate.get('note')}" if rate.get("note") else "")
                + "。该项为人工/外部录入，非系统自动采集，需自行核验时效性",
                f"外部录入：{rate_source}", as_of=rate.get("as_of"),
                ref="macro.rate", direction=direction,
                strength="弱" if direction != "neutral" else None,
            ))
        else:
            items.append(_missing(
                "M1", "宏观面", "利率是否有利于科技股？", "未接入利率数据源",
                "系统未接入政策利率/国债收益率/利率期货数据，不推断利率方向；"
                "如需纳入，请通过 analyze(macro={...}) 显式录入并注明来源与日期",
            ))

        industry = macro.get("industry") if isinstance(macro.get("industry"), dict) else {}
        ind_judgement = str(industry.get("judgement") or "").strip()
        ind_source = str(industry.get("source") or "").strip()
        if ind_judgement in MACRO_JUDGEMENT and ind_source:
            direction = MACRO_JUDGEMENT[ind_judgement]
            items.append(_make(
                "M2", "宏观面", "AI 行业景气度是否提升？",
                "是" if direction == "bull" else ("否" if direction == "bear" else "部分"),
                f"外部录入判定：{ind_judgement}"
                + (f"；依据 {industry.get('value')}" if industry.get("value") else "")
                + "。该项为人工/外部录入，非系统自动采集",
                f"外部录入：{ind_source}", as_of=industry.get("as_of"),
                ref="macro.industry", direction=direction,
                strength="弱" if direction != "neutral" else None,
            ))
        else:
            proxy = self._tech_sector_proxy(ctx)
            if proxy is None:
                items.append(_missing(
                    "M2", "宏观面", "AI 行业景气度是否提升？", "未接入产业景气度数据源",
                    "无产业订单/资本开支/景气指数数据，且本次扫描未取得科技/AI 相关板块行情；"
                    "景气度不作推断，也不以个股涨跌代替行业数据",
                ))
            else:
                up = proxy["avg_change_pct"] > 0
                items.append(_make(
                    "M2", "宏观面", "AI 行业景气度是否提升？",
                    "是（行情代理）" if up else "否（行情代理）",
                    f"科技/AI 相关板块 {proxy['count']} 个，平均涨幅 "
                    f"{proxy['avg_change_pct']:+.2f}%（{proxy['names']}）。"
                    "该指标仅为板块行情强弱的间接代理，不等于产业景气度、订单或资本开支数据，"
                    "也不代表本标的属于该行业",
                    "Futu 板块行情（强势板块扫描结果）", as_of=proxy.get("as_of"),
                    ref="sectors.change_pct（科技/AI 关键词匹配）",
                    value=proxy["avg_change_pct"],
                    direction="bull" if up else "bear", strength="弱",
                ))

        regime = str(market.get("market_regime") or "").strip()
        if regime and regime != "数据不足":
            bullish = any(k in regime for k in ("偏多", "强势", "普涨", "上行"))
            bearish = any(k in regime for k in ("偏空", "弱势", "普跌", "下行"))
            direction = "bull" if bullish else ("bear" if bearish else "neutral")
            adr = to_number(market.get("advance_decline_ratio")) or 0.0
            avg = to_number(market.get("avg_change_pct")) or 0.0
            items.append(_make(
                "M3", "宏观面", "市场整体环境是否配合？",
                "是" if bullish else ("否" if bearish else "部分"),
                f"市场判定：{regime}；上涨家数占比 {adr:.1%}，平均涨跌 {avg:.2f}%",
                "Futu 全市场快照聚合（市场 Agent）", as_of=market.get("as_of"),
                ref="market.market_regime", direction=direction,
                strength="中" if direction != "neutral" else None,
            ))
        else:
            items.append(_missing(
                "M3", "宏观面", "市场整体环境是否配合？", "市场 Agent",
                "本次分析未传入市场环境摘要，整体环境不作判断",
            ))
        return items

    @staticmethod
    def _tech_sector_proxy(ctx):
        rows = _rows(ctx.get("sectors"))
        matched = []
        for row in rows:
            name = str(row.get("sector_name") or row.get("name") or row.get("plate_name") or "")
            if not any(k in name.lower() for k in TECH_KEYWORDS):
                continue
            change = to_number(row.get("change_pct"))
            if change is None:
                continue
            matched.append((name, change))
        if not matched:
            return None
        return {
            "count": len(matched),
            "avg_change_pct": round(sum(x[1] for x in matched) / len(matched), 2),
            "names": "、".join(x[0] for x in matched[:3]),
            "as_of": "本次板块扫描",
        }


def build_evidence(reports, context=None):
    """模块级便捷入口。"""
    return DebateEvidenceBuilder().build(reports, context)
