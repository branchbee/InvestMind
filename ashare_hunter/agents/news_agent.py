"""新闻 Agent：公告、业绩预告、重大事件。

数据来自 Futu get_search_news（NOTICE 公告 / NEWS 资讯 / RATING 评级），
只呈现真实返回的标题、来源、时间与链接；用关键词做事件类型标记，
不判断消息真伪，不生成不存在的公告或业绩预告。
"""

import re
from datetime import datetime

EVENT_RULES = [
    ("业绩预告", ["业绩预告", "业绩预增", "业绩预减", "业绩快报", "预增", "预减", "扭亏", "首次盈利"]),
    ("重大合同/订单", ["中标", "重大合同", "重大订单", "签订", "框架协议"]),
    ("并购重组", ["重大资产重组", "收购", "并购", "资产重组", "吸收合并", "要约"]),
    ("增减持/回购", ["增持", "减持", "股份回购", "回购股份"]),
    ("再融资", ["定增", "非公开发行", "配股", "可转债", "发行股份"]),
    ("风险事项", ["立案", "处罚", "诉讼", "仲裁", "ST", "退市", "违规", "冻结", "质押", "问询函", "监管函"]),
    ("分红", ["分红", "派息", "送股", "除权除息"]),
]

BULLISH_WORDS = ["预增", "扭亏", "中标", "增持", "回购", "增长", "超预期", "盈利"]
BEARISH_WORDS = ["预减", "减持", "立案", "处罚", "诉讼", "亏损", "下滑", "问询", "退市", "风险"]


class NewsAgent:
    """归类公告与资讯，输出可追溯的事件线索而非主观结论。"""

    def run(self, notices=None, news=None, ratings=None, stock_name="", stock_code=""):
        notice_rows = self._rows(notices)
        news_rows = self._rows(news)
        rating_rows = self._rows(ratings)

        items = []
        seen = set()
        for source_type, rows in (("公告", notice_rows), ("资讯", news_rows), ("评级", rating_rows)):
            for row in rows:
                title = str(row.get("title") or "").strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                tags = self._classify(title)
                tone = self._tone(title)
                items.append({
                    "type": source_type,
                    "title": title,
                    "tags": tags,
                    "tone": tone,
                    "source": row.get("source"),
                    "publish_time": self._format_time(row.get("publish_time")),
                    "url": row.get("url"),
                    "news_sub_type": row.get("news_sub_type"),
                    "related_securities": row.get("related_securities"),
                })

        # 相关度过滤：get_search_news 是关键词搜索，可能混入其他公司的消息。
        # 公告的 related_securities 通常返回股票代码，而检索关键词是中文简称，
        # 因此必须同时用「名称 + 代码」匹配，否则会把本公司公告全部过滤掉。
        related = [x for x in items if self._is_related(x, stock_name, stock_code)]
        unrelated_count = len(items) - len(related)

        notice_tags = {}
        for item in related:
            if item["type"] == "公告":
                for tag in item["tags"]:
                    notice_tags[tag] = notice_tags.get(tag, 0) + 1

        bullish = sum(1 for x in related if x["tone"] == "偏正面")
        bearish = sum(1 for x in related if x["tone"] == "偏负面")
        score = None
        if related:
            score = max(20, min(85, 55 + (bullish - bearish) * 8))

        lines = []
        if related:
            for item in related[:8]:
                tag_text = "/".join(item["tags"]) if item["tags"] else "一般"
                line = f"[{item['type']}·{tag_text}·{item['tone']}] {item['title']}"
                if item["publish_time"]:
                    line += f"（{item['publish_time']}"
                    if item["source"]:
                        line += f"，{item['source']}"
                    line += "）"
                lines.append(line)
        if unrelated_count:
            lines.append(f"另有 {unrelated_count} 条关键词命中但与 {stock_name or '该股'} 无明显关联的资讯已过滤。")

        if related:
            conclusion = f"共取得 {len(related)} 条相关公告/资讯/评级线索；" + ("；".join(lines) if lines else "")
            conclusion += " 事件标签由标题关键词生成，需以公告原文核实，不构成消息真实性判断。"
        else:
            conclusion = (
                f"Futu 资讯接口未返回与 {stock_name or '该股'} 明确相关的公告、业绩预告或重大事件线索。"
                " 此处『未返回』不等于『不存在』，请以交易所公告为准。"
            )

        top_tags = sorted(notice_tags.items(), key=lambda kv: -kv[1])[:5]
        return {
            "agent": "新闻 Agent", "status": "完成" if related else "数据不足",
            "score": score, "items": related[:20], "top_tags": top_tags,
            "bullish_count": bullish, "bearish_count": bearish,
            "filtered_unrelated_count": unrelated_count,
            "conclusion": conclusion,
        }

    @staticmethod
    def _rows(bundle):
        if isinstance(bundle, dict) and bundle.get("available"):
            return bundle.get("rows") or []
        if isinstance(bundle, list):
            return bundle
        return []

    @staticmethod
    def _classify(title):
        matched = []
        for tag, keywords in EVENT_RULES:
            if any(re.search(re.escape(k), title) for k in keywords):
                matched.append(tag)
        return matched or ["一般信息"]

    @staticmethod
    def _tone(title):
        bull = sum(1 for w in BULLISH_WORDS if w in title)
        bear = sum(1 for w in BEARISH_WORDS if w in title)
        if bull > bear:
            return "偏正面"
        if bear > bull:
            return "偏负面"
        return "中性"

    @staticmethod
    def _format_time(value):
        if value is None:
            return ""
        text = str(value)
        # Futu 可能返回毫秒时间戳
        if re.fullmatch(r"\d{10,13}", text):
            ts = int(text) / (1000 if len(text) == 13 else 1)
            try:
                return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            except (ValueError, OSError, OverflowError):
                return text
        return text[:30]

    @staticmethod
    def _normalise_code(value):
        """提取证券代码主体：A 股为 6 位数字，美股为字母 ticker（如 AAPL / BRK.B）。

        优先匹配 A 股 6 位数字，避免把 "US.AAPL" 中的字母误判；
        美股形态支持 US.AAPL / AAPL.US / AAPL / BRK.B。
        """
        text = str(value or "").strip()
        match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
        if match:
            return match.group(1)
        # 先去掉市场前缀/后缀，再提取 ticker（BRK.B 这类多段代码需保留点号）
        text = re.sub(r"^(US|SH|SZ|HK)\.", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\.(US|HK|SH|SZ)$", "", text, flags=re.IGNORECASE)
        ticker = re.search(r"[A-Z][A-Z0-9]{0,5}(?:\.[A-Z]{1,2})?", text, re.IGNORECASE)
        return ticker.group(0).upper() if ticker else ""

    @staticmethod
    def _is_related(item, stock_name="", stock_code=""):
        """用「名称 + 代码」双重口径判断资讯是否属于目标个股。

        Futu 公告的 related_securities 通常只有代码，而检索关键词是中文简称，
        只看名称会把本公司公告误判为无关；只看代码又会在证券列表缺失时误杀。
        """
        name = str(stock_name or "").strip()
        code = NewsAgent._normalise_code(stock_code)
        if not name and not code:
            return True  # 没有任何标识时不做过滤，避免擅自丢弃数据

        related = item.get("related_securities") or []
        if isinstance(related, (str, dict)):
            related = [related]
        related = list(related) if isinstance(related, (list, tuple)) else []
        title = str(item.get("title") or "")

        if code:
            for entry in related:
                if isinstance(entry, dict):
                    candidates = [entry.get("code"), entry.get("symbol"), entry.get("security_code")]
                else:
                    candidates = [entry]
                if any(NewsAgent._normalise_code(c) == code for c in candidates):
                    return True
            if code in title:
                return True

        if name:
            if name in title:
                return True
            for entry in related:
                if isinstance(entry, dict):
                    related_name = str(entry.get("name") or entry.get("security_name") or "")
                    if related_name and (name in related_name or related_name in name):
                        return True
                    # 兼容名称/代码出现在证券条目文本中的情形（旧版按 str(related) 匹配）
                    if name in str(entry):
                        return True
                elif name in str(entry):
                    return True
        return False
