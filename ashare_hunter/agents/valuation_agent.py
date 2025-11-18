"""估值 Agent：PE、PB、PEG、历史估值分位。

PE / PB 来自 Futu 快照；历史估值分位来自 Futu get_valuation_detail 的
valuation_percentile 字段（真实计算的 0-100 分位）；PEG 由 PE-TTM 与
净利润同比增速计算，增速为负或缺失时不计算 PEG，避免指标失真。
"""

from .fundamental_base import to_number, score_by_thresholds, score_label, fmt_pct

# 估值分位：低于 30% 偏低，30%-70% 中性，高于 70% 偏高（方法论参考阈值）
PERCENTILE_THRESHOLDS = [(70, 30), (30, 55), (0, 85)]


class ValuationAgent:
    """基于快照与估值详情输出结构化估值信号。"""

    def run(self, snapshot=None, pe_detail=None, pb_detail=None, ps_detail=None,
            profit_growth_pct=None):
        snap = snapshot if isinstance(snapshot, dict) and snapshot.get("available") else {}
        pe = to_number(snap.get("pe_ttm_ratio")) or to_number(snap.get("pe_ratio"))
        pb = to_number(snap.get("pb_ratio"))
        ps = to_number(snap.get("ps_ratio"))

        percentile = self._percentile(pe_detail)
        pe_average = self._trend_value(pe_detail, "average_value")
        pe_current_detail = self._trend_value(pe_detail, "current_value")
        pe_hist_count = self._history_count(pe_detail)
        pb_percentile = self._percentile(pb_detail)

        # PEG：仅当 PE>0 且净利润增速>0 时计算（增速用百分数）
        peg = None
        peg_reason = None
        if pe is not None and pe > 0:
            if profit_growth_pct is None:
                peg_reason = "缺少净利润同比增速，无法计算 PEG"
            elif profit_growth_pct <= 0:
                peg_reason = f"净利润同比 {profit_growth_pct:.2f}% ≤ 0，PEG 不适用"
            else:
                peg = pe / profit_growth_pct
        else:
            peg_reason = "PE 缺失或非正，无法计算 PEG"

        # 打分：分位优先，退而用 PE/PB 绝对水平；可用维度平均
        scores = []
        if percentile is not None:
            scores.append(score_by_thresholds(percentile, PERCENTILE_THRESHOLDS, higher_is_better=False))
        if peg is not None:
            # PEG<1 优、<2 中性、>=2 偏高（越低越好）
            scores.append(score_by_thresholds(peg, [(2, 35), (1, 60), (0, 85)], higher_is_better=False))
        if scores:
            score = round(sum(scores) / len(scores), 1)
        elif pe is not None and pe > 0:
            score = score_by_thresholds(pe, [(60, 30), (30, 50), (15, 70), (0, 85)], higher_is_better=False)
        else:
            score = None

        dimensions = {
            "pe_ttm": {"value": pe, "field": "pe_ttm_ratio / pe_ratio", "score": None},
            "pb": {"value": pb, "field": "pb_ratio", "score": None},
            "ps": {"value": ps, "field": "ps_ratio", "score": None},
            "pe_historical_percentile": {
                "value": percentile, "score": score_by_thresholds(percentile, PERCENTILE_THRESHOLDS, higher_is_better=False)
                if percentile is not None else None,
                "label": self._percentile_label(percentile),
                "history_count": pe_hist_count, "average": pe_average,
            },
            "pb_historical_percentile": {"value": pb_percentile, "label": self._percentile_label(pb_percentile)},
            "peg": {"value": peg, "reason": peg_reason, "growth_pct_used": profit_growth_pct,
                    "label": score_label(score_by_thresholds(peg, [(2, 35), (1, 60), (0, 85)], higher_is_better=False))
                    if peg is not None else "无法计算"},
        }

        parts = [f"PE(TTM) {fmt_pct(pe)}", f"PB {fmt_pct(pb)}", f"PS {fmt_pct(ps)}"]
        if percentile is not None:
            parts.append(f"PE 历史分位 {percentile:.1f}%（{self._percentile_label(percentile)}，近5年口径）")
        else:
            parts.append("PE 历史分位：数据不足")
        if pb_percentile is not None:
            parts.append(f"PB 历史分位 {pb_percentile:.1f}%")
        if peg is not None:
            parts.append(f"PEG {peg:.2f}（按 PE-TTM / 净利润同比计算）")
        else:
            parts.append(f"PEG 无法计算：{peg_reason}")
        if pe_average is not None:
            parts.append(f"近5年 PE 均值 {pe_average:.2f}")

        conclusion = "；".join(parts) + "。"
        if snap:
            conclusion += f" 快照更新时间：{snap.get('update_time') or '未提供'}（来源：Futu 行情快照）。"
        if pe_detail and isinstance(pe_detail, dict) and pe_detail.get("available"):
            detail = pe_detail.get("detail", {})
            trend = detail.get("trend", {})
            if trend.get("last_update_time_str"):
                conclusion += f" 估值分位数据时间：{trend['last_update_time_str']}。"
        conclusion += " 低估值不等同于便宜，需结合盈利质量与行业属性判断。"

        status = "完成" if (pe is not None or pb is not None or percentile is not None) else "数据不足"
        if status == "数据不足":
            conclusion = "Futu 未返回可核验的 PE/PB/估值分位数据，估值区间不作判断，不生成虚构数值。"

        return {
            "agent": "估值 Agent", "status": status, "score": score, "dimensions": dimensions,
            "pe_ttm": pe, "pb": pb, "ps": ps,
            "pe_percentile": percentile, "pb_percentile": pb_percentile, "peg": peg,
            "conclusion": conclusion,
        }

    @staticmethod
    def _percentile(detail):
        if not isinstance(detail, dict) or not detail.get("available"):
            return None
        trend = (detail.get("detail") or {}).get("trend") or {}
        value = to_number(trend.get("valuation_percentile"))
        if value is None:
            return None
        # 兼容 0-1 与 0-100 两种口径
        return value * 100 if 0 < value <= 1 else value

    @staticmethod
    def _trend_value(detail, key):
        if not isinstance(detail, dict) or not detail.get("available"):
            return None
        return to_number(((detail.get("detail") or {}).get("trend") or {}).get(key))

    @staticmethod
    def _history_count(detail):
        if not isinstance(detail, dict) or not detail.get("available"):
            return 0
        trend = (detail.get("detail") or {}).get("trend") or {}
        return len(trend.get("historical_items") or [])

    @staticmethod
    def _percentile_label(value):
        if value is None:
            return "数据不足"
        if value < 30:
            return "历史偏低"
        if value <= 70:
            return "历史中性"
        return "历史偏高"
