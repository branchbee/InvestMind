"""资金 Agent：主力资金、机构持仓、成交结构。

主力/超大/大/中/小单来自 Futu get_capital_flow 与 get_capital_distribution；
机构持股来自 get_shareholders_institutional，分析师评级仅作机构观点补充，
不等同于机构持仓。成交额、换手率只代表活跃度，不等于净流入。
"""

from .fundamental_base import to_number, fmt_pct

LARGE_ORDER_KEYS = ("super_in_flow", "big_in_flow", "main_in_flow")
DUST_KEYS = ("mid_in_flow", "sml_in_flow")


class CapitalAgent:
    """汇总真实资金流字段，明确口径，缺失项不作推断。"""

    def run(self, capital_flow=None, distribution=None,
            institutional=None, analyst_consensus=None):
        flow_rows = self._rows(capital_flow)
        dist = distribution.get("distribution") if isinstance(distribution, dict) and distribution.get("available") else {}

        # 日频资金流：取最近一日与近 N 日累计
        latest = flow_rows[-1] if flow_rows else {}
        latest_flows = {k: to_number(latest.get(k)) for k in
                        ("in_flow", "super_in_flow", "big_in_flow", "mid_in_flow", "sml_in_flow", "main_in_flow")}
        flow_fields = [k for k in LARGE_ORDER_KEYS if latest_flows.get(k) is not None]
        main_value = latest_flows.get("main_in_flow")
        if main_value is None and latest_flows.get("super_in_flow") is not None and latest_flows.get("big_in_flow") is not None:
            main_value = latest_flows["super_in_flow"] + latest_flows["big_in_flow"]

        cumulative = {}
        for key in ("main_in_flow", "super_in_flow", "big_in_flow", "in_flow"):
            values = [to_number(r.get(key)) for r in flow_rows]
            values = [v for v in values if v is not None]
            if values:
                cumulative[key] = sum(values)

        dist_flows = {k: to_number(dist.get(k)) for k in (
            "capital_in_super", "capital_in_big", "capital_in_mid", "capital_in_small",
            "capital_out_super", "capital_out_big", "capital_out_mid", "capital_out_small",
        )}
        large_net = None
        if all(dist_flows.get(k) is not None for k in ("capital_in_super", "capital_out_super",
                                                       "capital_in_big", "capital_out_big")):
            large_net = (dist_flows["capital_in_super"] + dist_flows["capital_in_big"]
                         - dist_flows["capital_out_super"] - dist_flows["capital_out_big"])

        inst_rows = self._rows(institutional)
        inst_count = len(inst_rows)
        consensus_rows = self._rows(analyst_consensus)
        consensus_count = len(consensus_rows)

        # 打分：主力净流入为正给偏高分，负给偏低分；无主力数据时用资金分布补充
        direction_value = main_value if main_value is not None else large_net
        if direction_value is None and "main_in_flow" in cumulative:
            direction_value = cumulative["main_in_flow"]
        if direction_value is None:
            score = None
        elif direction_value > 0:
            score = 75 if direction_value >= 1e7 else 65
        elif direction_value < 0:
            score = 35 if direction_value <= -1e7 else 45
        else:
            score = 50
        if inst_count and score is not None:
            score = min(90, score + 5)  # 有机构持仓数据仅作轻微信息增量，不夸大

        parts = []
        if main_value is not None:
            parts.append(f"最近一日主力净流入 {self._yi(main_value)} 元")
        elif flow_fields:
            parts.append("接口未提供 main_in_flow，仅有分项：" + "、".join(flow_fields))
        else:
            parts.append("主力资金：数据不足")
        if "main_in_flow" in cumulative:
            parts.append(f"近 {len(flow_rows)} 日主力累计 {self._yi(cumulative['main_in_flow'])} 元")
        if large_net is not None:
            parts.append(f"成交结构中特大+大单净额 {self._yi(large_net)} 元")
        if dist.get("update_time"):
            parts.append(f"资金分布更新于 {dist['update_time']}")

        if isinstance(institutional, dict) and not institutional.get("available"):
            parts.append(f"机构持股：未取得（{institutional.get('reason', '接口未返回')}）")
        elif inst_count:
            parts.append(f"机构持股记录 {inst_count} 条（来源：Futu 机构持股接口，需以定期报告核验）")
        else:
            parts.append("机构持股：接口未返回可核验记录")

        if isinstance(analyst_consensus, dict) and not analyst_consensus.get("available"):
            parts.append("分析师评级：未取得")
        elif consensus_count:
            parts.append(f"分析师评级记录 {consensus_count} 条（评级为观点，不等同于持仓）")
        else:
            parts.append("分析师评级：接口未返回记录")

        conclusion = "；".join(parts) + "。"
        conclusion += " 资金流为交易行为统计，不代表趋势可持续性；成交额和换手率不等于净流入。"

        return {
            "agent": "资金 Agent", "status": "完成" if (flow_rows or dist or inst_count or consensus_count) else "数据不足",
            "score": score,
            "main_net_inflow_latest": main_value,
            "main_net_inflow_cumulative": cumulative.get("main_in_flow"),
            "large_order_net_from_distribution": large_net,
            "institutional_holder_count": inst_count if inst_count else None,
            "analyst_rating_count": consensus_count if consensus_count else None,
            "flow_days": len(flow_rows),
            "conclusion": conclusion,
        }

    @staticmethod
    def _rows(bundle):
        if isinstance(bundle, dict):
            if not bundle.get("available"):
                return []
            return bundle.get("rows") or []
        return bundle if isinstance(bundle, list) else []

    @staticmethod
    def _yi(value):
        if value is None:
            return "数据不足"
        return f"{value / 1e8:.4f}亿"
