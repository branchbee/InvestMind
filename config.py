from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    top_sectors: int = int(os.getenv("TOP_SECTORS", "10"))
    top_stocks: int = int(os.getenv("TOP_STOCKS", "30"))
    min_amount_yuan: float = float(os.getenv("MIN_AMOUNT_YUAN", "50000000"))

    # Futu OpenD
    futu_host: str = os.getenv("FUTU_HOST", "127.0.0.1")
    futu_port: int = int(os.getenv("FUTU_PORT", "11111"))
    futu_max_plate_refresh_per_run: int = int(os.getenv("FUTU_MAX_PLATE_REFRESH_PER_RUN", "10"))
    futu_plate_cache_days: int = int(os.getenv("FUTU_PLATE_CACHE_DAYS", "7"))
    futu_request_interval: float = float(os.getenv("FUTU_REQUEST_INTERVAL", "0.60"))

    # Optional DeepSeek-compatible LLM
    llm_enabled: bool = os.getenv("LLM_ENABLED", "false").lower() == "true"
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "deepseek-chat")

    # 基本面 / 估值 / 新闻 / 资金 Agent 取数配置
    financial_report_count: int = int(os.getenv("FINANCIAL_REPORT_COUNT", "5"))
    valuation_interval: str = os.getenv("VALUATION_INTERVAL", "YEAR_5")
    news_max_count: int = int(os.getenv("NEWS_MAX_COUNT", "20"))
    capital_flow_days: int = int(os.getenv("CAPITAL_FLOW_DAYS", "10"))
    # 决策 Agent 维度权重，取值 0-1；缺失维度自动从分母中剔除
    decision_weight_financial: float = float(os.getenv("DECISION_WEIGHT_FINANCIAL", "0.25"))
    decision_weight_valuation: float = float(os.getenv("DECISION_WEIGHT_VALUATION", "0.25"))
    decision_weight_news: float = float(os.getenv("DECISION_WEIGHT_NEWS", "0.15"))
    decision_weight_capital: float = float(os.getenv("DECISION_WEIGHT_CAPITAL", "0.20"))
    decision_weight_technical: float = float(os.getenv("DECISION_WEIGHT_TECHNICAL", "0.15"))

    # 美股研究：全市场板块扫描（与 A 股链路对称，不使用观察池）
    us_top_sectors: int = int(os.getenv("US_TOP_SECTORS", "10"))
    us_top_stocks: int = int(os.getenv("US_TOP_STOCKS", "30"))
    us_min_amount_usd: float = float(os.getenv("US_MIN_AMOUNT_USD", "10000000"))
    # 美股 OTC（粉单）等标的通常无基础行情权限，混入快照批次会导致整批失败。
    # 默认从全市场静态信息中剔除；关闭后仍由批次容错自动隔离坏代码。
    us_exclude_otc: bool = os.getenv("US_EXCLUDE_OTC", "true").lower() == "true"
    # 快照单批代码数（富途上限 400）；调小可降低单批受坏标的影响的范围
    us_snapshot_batch_size: int = int(os.getenv("US_SNAPSHOT_BATCH_SIZE", "400"))

    # 调试日志：LOG_LEVEL=DEBUG 可打开 Futu 每次调用、阶段耗时与完整堆栈
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_file: str = os.getenv("LOG_FILE", "output/logs/investmind.log")
    log_console_level: str = os.getenv("LOG_CONSOLE_LEVEL", "INFO")
    log_gui_level: str = os.getenv("LOG_GUI_LEVEL", "INFO")
    log_third_party_level: str = os.getenv("LOG_THIRD_PARTY_LEVEL", "WARNING")
    log_max_bytes: int = int(os.getenv("LOG_MAX_BYTES", "5242880"))
    log_backup_count: int = int(os.getenv("LOG_BACKUP_COUNT", "3"))

    def decision_weights(self) -> dict:
        return {
            "财务": self.decision_weight_financial,
            "估值": self.decision_weight_valuation,
            "新闻": self.decision_weight_news,
            "资金": self.decision_weight_capital,
            "技术面": self.decision_weight_technical,
        }


settings = Settings()
