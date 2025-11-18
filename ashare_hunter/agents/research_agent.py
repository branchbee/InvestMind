import json
import requests
from config import settings

class ResearchAgent:
    def explain(self, market, sectors, stocks):
        if not settings.llm_enabled or not settings.llm_api_key:
            return "LLM未启用。当前排名仅由量化规则生成；财务、公告、新闻和资金流向尚未纳入本版筛选。"
        payload = {
            "model": settings.llm_model,
            "temperature": 0.2,
            "messages": [{"role": "user", "content":
                "你是审慎的A股研究助理。只根据输入数据解释市场环境、板块和候选股；不得编造财务数据、资金流向、新闻或目标价，缺失写未知。强调不是买卖建议。\n" +
                json.dumps({"market": market, "sectors": sectors.head(10).to_dict("records"),
                            "stocks": stocks.head(15).to_dict("records")}, ensure_ascii=False, default=str)}]
        }
        try:
            r = requests.post(settings.llm_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + settings.llm_api_key}, json=payload, timeout=60)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            return f"LLM解读调用失败：{exc}；量化结果仍可查看。"
