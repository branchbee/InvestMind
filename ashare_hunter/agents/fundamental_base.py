"""基本面类 Agent 的共用工具：字段匹配、数值解析、阈值打分。

设计原则：
1. 财务字段名以 Futu 返回的 display_name 为准，用别名模糊匹配，不做字段硬编码。
2. 任何缺失值一律返回 None，绝不用 0 或猜测值填充。
3. 打分阈值来自通用研究经验，属于规则化信号，不代表收益概率。
"""

import math
import re


CONTROL_CHARS = re.compile(r"[\s\(\)（）%％,，。:：/\\·\-—]+")


def to_number(value):
    """把可能带单位/千分位的字段值转成 float，失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "na", "none", "--", "-", "null", "nan"}:
        return None
    multiplier = 1.0
    if text.endswith("万亿"):
        multiplier, text = 1e12, text[:-2]
    elif text.endswith("亿"):
        multiplier, text = 1e8, text[:-1]
    elif text.endswith("万"):
        multiplier, text = 1e4, text[:-1]
    cleaned = text.replace(",", "").replace("，", "").replace("%", "").replace("％", "")
    cleaned = re.sub(r"[^\d\.\+\-eE]", "", cleaned)
    if not cleaned or cleaned in {"+", "-", ".", "e", "E"}:
        return None
    try:
        result = float(cleaned) * multiplier
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def normalise_name(text):
    """归一化 display_name 以便别名匹配。"""
    return CONTROL_CHARS.sub("", str(text or "")).lower()


def find_item(report, aliases):
    """在单期 item_list 中按别名字面匹配字段，返回匹配到的 item 或 None。"""
    items = (report or {}).get("item_list") or []
    alias_keys = [normalise_name(a) for a in aliases if str(a).strip()]
    # 精确匹配优先，其次包含匹配；避免"净利润"误匹配到"扣非净利润"之外的字段时保持顺序敏感
    for alias in alias_keys:
        for item in items:
            if normalise_name(item.get("display_name")) == alias:
                return item
    for alias in alias_keys:
        for item in items:
            name = normalise_name(item.get("display_name"))
            if name and alias and (alias in name or name in alias):
                return item
    return None


def pick_metric(reports, aliases, use_yoy=True):
    """从最新一期开始查找指标，返回最近可核验的值与同比。

    返回 dict: value/yoy/period/field；无数据返回 None。
    """
    for report in reports or []:
        item = find_item(report, aliases)
        if item is None:
            continue
        value = to_number(item.get("data"))
        yoy = to_number(item.get("yoy")) if use_yoy else None
        if value is None and yoy is None:
            continue
        return {
            "value": value,
            "yoy": yoy,
            "period": report.get("date_time_str") or report.get("period_text") or "未知报告期",
            "field": item.get("display_name") or "未知字段",
            "fiscal_type": report.get("financial_type"),
        }
    return None


def compute_yoy(reports, aliases):
    """当接口未直接给 yoy 时，按同口径相邻报告期计算同比。"""
    series = []
    for report in reports or []:
        item = find_item(report, aliases)
        if item is None:
            continue
        value = to_number(item.get("data"))
        if value is not None:
            series.append((report.get("date_time_str") or report.get("period_text") or "", value))
    if len(series) < 2:
        return None
    # reports 通常按期倒序（最新在前）
    newer, older = series[0], series[1]
    if older[1] == 0:
        return None
    return {
        "yoy": (newer[1] / older[1] - 1) * 100,
        "from_period": older[0],
        "to_period": newer[0],
    }


def score_by_thresholds(value, thresholds, higher_is_better=True):
    """按 [(边界, 分数), ...] 打分。thresholds 需按边界升序排列。

    value 为 None 时返回 None，交由上层标记数据不足。
    """
    if value is None:
        return None
    for boundary, score in thresholds:
        if (value >= boundary) if higher_is_better else (value <= boundary):
            return score
    return thresholds[-1][1]


def score_label(score):
    if score is None:
        return "数据不足"
    if score >= 80:
        return "强"
    if score >= 65:
        return "偏强"
    if score >= 45:
        return "中性"
    if score >= 30:
        return "偏弱"
    return "弱"


def fmt_pct(value, digits=2):
    return "数据不足" if value is None else f"{value:.{digits}f}%"


def fmt_num(value, digits=2):
    return "数据不足" if value is None else f"{value:.{digits}f}"
