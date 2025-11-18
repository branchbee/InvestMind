from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import futu as ft

logger = logging.getLogger(__name__)


class FutuProvider:
    """
    Futu OpenD 数据适配层。

    说明：
    1. OpenD 必须先启动，默认监听 127.0.0.1:11111。
    2. 行情权限由 Futu 账号及市场权限决定。
    3. 板块成分股保存在 SQLite 中，按需增量刷新，避免每次全量请求。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        cache_db: str = "output/futu_cache.sqlite3",
        max_plate_refresh_per_run: int = 10,
        plate_cache_days: int = 7,
        request_interval: float = 0.60,
    ):
        self.host = host
        self.port = int(port)
        self.cache_db = Path(cache_db)
        self.cache_db.parent.mkdir(parents=True, exist_ok=True)
        self.max_plate_refresh_per_run = int(max_plate_refresh_per_run)
        self.plate_cache_days = int(plate_cache_days)
        self.request_interval = max(float(request_interval), 0.0)
        self._last_request_at = 0.0
        self._last_plate_request_at = 0.0
        self._quote_ctx = None
        self._spot_cache: Optional[pd.DataFrame] = None
        self._ensure_db()

    def _ensure_db(self):
        with sqlite3.connect(self.cache_db) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS plate_members (
                    plate_code TEXT NOT NULL,
                    plate_name TEXT NOT NULL,
                    board_type TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (plate_code, stock_code)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_plate_members_name
                ON plate_members(plate_name, board_type)
            """)

    def _ctx(self):
        if self._quote_ctx is None:
            self._quote_ctx = ft.OpenQuoteContext(host=self.host, port=self.port)
        return self._quote_ctx

    def close(self):
        if self._quote_ctx is not None:
            try:
                self._quote_ctx.close()
            finally:
                self._quote_ctx = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _pace(self):
        elapsed = time.monotonic() - self._last_request_at
        wait = self.request_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _pace_plate(self):
        # get_plate_list / get_plate_stock 官方限制均为每 30 秒最多 10 次。
        elapsed = time.monotonic() - self._last_plate_request_at
        wait = 3.1 - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_plate_request_at = time.monotonic()

    def _call(self, method_name: str, *args, **kwargs):
        if method_name in {"get_plate_list", "get_plate_stock"}:
            self._pace_plate()
        else:
            self._pace()
        method = getattr(self._ctx(), method_name)
        ret, data = method(*args, **kwargs)
        if ret != ft.RET_OK:
            raise RuntimeError(f"Futu {method_name} 调用失败：{data}")
        return data

    @staticmethod
    def _normalise_code(code: str) -> str:
        value = str(code).strip()
        if "." in value:
            return value
        value = value.zfill(6)
        if value.startswith(("5", "6", "9")):
            return f"SH.{value}"
        return f"SZ.{value}"

    @staticmethod
    def _code6(code: str) -> str:
        return str(code).split(".")[-1].zfill(6)

    def _get_all_a_share_basic(self) -> pd.DataFrame:
        frames = []
        for market in (ft.Market.SH, ft.Market.SZ):
            data = self._call(
                "get_stock_basicinfo",
                market,
                stock_type=ft.SecurityType.STOCK
            )
            if data is not None and not data.empty:
                frames.append(data)
        if not frames:
            raise RuntimeError("Futu 未返回沪深 A 股静态信息，请检查 OpenD 连接和 A 股行情权限。")
        basic = pd.concat(frames, ignore_index=True).drop_duplicates("code")
        basic = basic[basic["code"].astype(str).str.startswith(("SH.", "SZ."))].copy()
        if "name" not in basic:
            basic["name"] = ""
        basic["code6"] = basic["code"].map(self._code6)
        return basic

    def get_spot(self) -> pd.DataFrame:
        """获取沪深 A 股快照；单次最多 400 个标的，按批次请求。"""
        basic = self._get_all_a_share_basic()
        codes = basic["code"].astype(str).tolist()
        frames = []
        for start in range(0, len(codes), 400):
            batch = codes[start:start + 400]
            snap = self._call("get_market_snapshot", batch)
            if snap is not None and not snap.empty:
                frames.append(snap)

        if not frames:
            raise RuntimeError("Futu 行情快照为空，请确认账号有 A 股行情权限。")

        quote = pd.concat(frames, ignore_index=True)
        df = basic.merge(quote, on="code", how="inner", suffixes=("_basic", ""))
        if "name" not in df:
            df["name"] = df.get("name_basic", "")
        elif "name_basic" in df:
            df["name"] = df["name"].fillna(df["name_basic"])

        # 统一为原 Agent 使用的字段名
        rename = {
            "turnover": "amount",
            "turnover_rate": "turnover",
            "last_price": "price",
            "volume": "volume",
        }
        for src, dst in rename.items():
            if src in df.columns:
                df[dst] = df[src]

        # Futu 快照文档提供 last_price / prev_close_price，涨跌幅由二者计算。
        # 不依赖 AKShare 的“涨跌幅”字段，也不把缺失值填成虚构涨幅。
        for col in ("price", "prev_close_price", "amount", "turnover", "volume"):
            if col not in df:
                df[col] = pd.NA
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if "change_pct" not in df:
            df["change_pct"] = pd.NA
        valid_prev = df["prev_close_price"].gt(0)
        df.loc[valid_prev, "change_pct"] = (
            (df.loc[valid_prev, "price"] / df.loc[valid_prev, "prev_close_price"] - 1) * 100
        )
        if "volume_ratio" not in df:
            df["volume_ratio"] = pd.NA
        df["volume_ratio"] = pd.to_numeric(df["volume_ratio"], errors="coerce")

        df["code6"] = df["code"].map(self._code6)
        df["code"] = df["code6"]
        df["name"] = df["name"].fillna("").astype(str)
        self._spot_cache = df.copy()
        return df

    def _get_plate_list(self, board_type: str) -> pd.DataFrame:
        plate_class = ft.Plate.INDUSTRY if board_type == "industry" else ft.Plate.CONCEPT
        # Futu 文档说明沪深市场板块列表不区分 SH/SZ，传任一市场即可返回沪深子板块
        data = self._call("get_plate_list", ft.Market.SH, plate_class)
        if data is None or data.empty:
            return pd.DataFrame(columns=["code", "plate_name", "board_type"])
        data = data.copy()
        data["board_type"] = board_type
        return data

    def _cached_plate_codes(self) -> set[str]:
        with sqlite3.connect(self.cache_db) as conn:
            rows = conn.execute("SELECT DISTINCT plate_code FROM plate_members").fetchall()
        return {r[0] for r in rows}

    def _refresh_plate_members(self, plate: dict, board_type: str):
        plate_code = str(plate["code"])
        plate_name = str(plate["plate_name"])
        members = self._call("get_plate_stock", plate_code)
        if members is None:
            members = pd.DataFrame()
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        if not members.empty:
            for _, row in members.iterrows():
                code = str(row.get("code", ""))
                if not code.startswith(("SH.", "SZ.")):
                    continue
                rows.append((
                    plate_code, plate_name, board_type, code,
                    str(row.get("stock_name", "")), now
                ))
        with sqlite3.connect(self.cache_db) as conn:
            conn.execute("DELETE FROM plate_members WHERE plate_code = ?", (plate_code,))
            if rows:
                conn.executemany("""
                    INSERT OR REPLACE INTO plate_members
                    (plate_code, plate_name, board_type, stock_code, stock_name, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, rows)

    def _load_members(self, plate_code: str) -> pd.DataFrame:
        with sqlite3.connect(self.cache_db) as conn:
            df = pd.read_sql_query(
                "SELECT stock_code AS code, stock_name AS name FROM plate_members WHERE plate_code = ?",
                conn, params=(plate_code,)
            )
        return df

    def get_sectors(self, spot: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        """
        获取板块列表，并通过板块成分股的当日快照聚合板块表现。
        首次运行只刷新 max_plate_refresh_per_run 个板块；后续运行逐步补齐。
        设置 FUTU_MAX_PLATE_REFRESH_PER_RUN=0 可尝试全量刷新（耗时较长）。
        """
        spot = spot if spot is not None else self._spot_cache
        if spot is None or spot.empty:
            raise RuntimeError("请先调用 get_spot() 获取行情快照，再计算板块强度。")

        plate_frames = [
            self._get_plate_list("industry"),
            self._get_plate_list("concept")
        ]
        plates = pd.concat(plate_frames, ignore_index=True)
        if plates.empty:
            raise RuntimeError("Futu 未返回板块列表。")

        cached_codes = self._cached_plate_codes()
        now = datetime.now()
        refresh_candidates = []
        for _, p in plates.iterrows():
            code = str(p["code"])
            if code not in cached_codes:
                refresh_candidates.append(p.to_dict())
                continue
            # 缓存超过配置天数时进入增量刷新队列
            with sqlite3.connect(self.cache_db) as conn:
                row = conn.execute(
                    "SELECT MAX(updated_at) FROM plate_members WHERE plate_code = ?", (code,)
                ).fetchone()
            if row and row[0]:
                try:
                    age = now - datetime.fromisoformat(row[0])
                    if age > timedelta(days=self.plate_cache_days):
                        refresh_candidates.append(p.to_dict())
                except ValueError:
                    refresh_candidates.append(p.to_dict())

        limit = self.max_plate_refresh_per_run
        selected = refresh_candidates if limit <= 0 else refresh_candidates[:limit]
        for plate in selected:
            try:
                self._refresh_plate_members(plate, plate["board_type"])
            except Exception as exc:
                logger.warning("板块成分股刷新失败 %s：%s", plate.get("plate_name"), exc)

        # 当前行情以 code6 对齐；成分股表保留完整 Futu 代码
        quotes = spot.copy()
        quotes["futu_code"] = quotes["code"].map(self._normalise_code)
        quote_map = quotes.set_index("futu_code", drop=False)

        results = []
        for _, p in plates.iterrows():
            code = str(p["code"])
            members = self._load_members(code)
            if members.empty:
                continue
            members = members.drop_duplicates("code")
            valid = members[members["code"].isin(quote_map.index)]
            if valid.empty:
                continue
            q = quote_map.loc[valid["code"].tolist()]
            changes = pd.to_numeric(q["change_pct"], errors="coerce").dropna()
            amounts = pd.to_numeric(q["amount"], errors="coerce").fillna(0)
            up_count = int((changes > 0).sum())
            down_count = int((changes < 0).sum())
            leader_idx = changes.idxmax() if not changes.empty else None
            leader_change = float(changes.max()) if not changes.empty else 0.0
            leader_name = ""
            if leader_idx is not None:
                try:
                    leader_name = str(quote_map.loc[leader_idx]["name"])
                except Exception:
                    pass
            results.append({
                "sector_name": str(p["plate_name"]),
                "board_type": str(p["board_type"]),
                "change_pct": float(changes.mean()) if not changes.empty else 0.0,
                "up_count": up_count,
                "down_count": down_count,
                "amount": float(amounts.sum()),
                "leader_change_pct": leader_change,
                "leader_name": leader_name,
                "member_count": int(len(valid)),
                "cache_coverage": round(len(valid) / max(len(members), 1), 4),
                "plate_code": code,
            })

        if not results:
            raise RuntimeError(
                "尚无可用板块成分股缓存。请增加 FUTU_MAX_PLATE_REFRESH_PER_RUN，"
                "或设置为 0 执行全量初始化（受 Futu 接口频率限制，可能耗时较长）。"
            )
        return pd.DataFrame(results)

    def get_sector_stocks(self, name: str, kind: str) -> pd.DataFrame:
        with sqlite3.connect(self.cache_db) as conn:
            rows = pd.read_sql_query(
                """SELECT DISTINCT stock_code AS futu_code, stock_name AS name
                   FROM plate_members WHERE plate_name = ? AND board_type = ?""",
                conn, params=(name, kind)
            )
        if rows.empty:
            raise RuntimeError(f"板块 {name} 暂无本地成分股缓存，请先运行板块缓存初始化。")

        rows["code"] = rows["futu_code"].map(self._code6)
        if self._spot_cache is None:
            spot = self.get_spot()
        else:
            spot = self._spot_cache
        keep = [c for c in ["code", "name", "change_pct", "amount", "turnover",
                            "volume_ratio", "price", "volume"] if c in spot.columns]
        result = rows[["code", "name"]].merge(
            spot[keep].drop_duplicates("code"), on="code", how="left", suffixes=("", "_quote")
        )
        if "name_quote" in result:
            result["name"] = result["name"].replace("", pd.NA).fillna(result["name_quote"])
            result = result.drop(columns=["name_quote"])
        return result

    def get_daily_history(self, code: str, days: int = 45) -> pd.DataFrame:
        futu_code = self._normalise_code(code)
        end = datetime.now().date()
        start = end - timedelta(days=days * 2 + 10)
        self._pace()
        ret, data, page_key = self._ctx().request_history_kline(
            futu_code,
            start=start.isoformat(),
            end=end.isoformat(),
            ktype=ft.KLType.K_DAY,
            autype=ft.AuType.QFQ,
            max_count=1000,
        )
        if ret != ft.RET_OK:
            raise RuntimeError(f"Futu 历史 K 线获取失败 {futu_code}：{data}")
        frames = [data] if data is not None and not data.empty else []
        # 当前请求范围一般少于 1000 根；若有分页则继续取完
        while page_key is not None:
            self._pace()
            ret, page, page_key = self._ctx().request_history_kline(
                futu_code,
                start=start.isoformat(),
                end=end.isoformat(),
                ktype=ft.KLType.K_DAY,
                autype=ft.AuType.QFQ,
                max_count=1000,
                page_req_key=page_key,
            )
            if ret != ft.RET_OK:
                raise RuntimeError(f"Futu 历史 K 线分页失败 {futu_code}：{page}")
            if page is not None and not page.empty:
                frames.append(page)
        if not frames:
            return pd.DataFrame(columns=["date", "close", "pct_change"])
        hist = pd.concat(frames, ignore_index=True).drop_duplicates("time_key")
        hist = hist.rename(columns={"time_key": "date", "change_rate": "pct_change"})
        for col in ("close", "pct_change"):
            if col in hist:
                hist[col] = pd.to_numeric(hist[col], errors="coerce")
        return hist.sort_values("date").reset_index(drop=True)

    # ------------------------------------------------------------------
    # 基本面 / 估值 / 资金 / 新闻 数据接口
    # ------------------------------------------------------------------
    def get_single_snapshot(self, code: str) -> dict:
        """单只股票快照，含 PE / PB / PE-TTM / 市值 / 每股收益等估值字段。

        快照字段以 Futu 实际返回为准，缺失字段保留 None，绝不填 0 代替缺失。
        """
        futu_code = self._normalise_code(code)
        data = self._call("get_market_snapshot", [futu_code])
        if data is None or getattr(data, "empty", True):
            return {"available": False, "reason": "快照为空"}
        row = data.iloc[0].to_dict()

        def num(key):
            if key not in row:
                return None
            value = pd.to_numeric(pd.Series([row[key]]), errors="coerce").iloc[0]
            return float(value) if pd.notna(value) else None

        return {
            "available": True,
            "code": futu_code,
            "price": num("last_price"),
            "pe_ratio": num("pe_ratio"),
            "pe_ttm_ratio": num("pe_ttm_ratio"),
            "pb_ratio": num("pb_ratio"),
            "ps_ratio": num("ps_ratio"),
            "net_asset_per_share": num("net_asset_per_share"),
            "earnings_per_share": num("earnings_pershare"),
            "dividend_ratio_ttm": num("dividend_ratio_ttm"),
            "total_market_val": num("total_market_val"),
            "circular_market_val": num("circular_market_val"),
            "update_time": str(row.get("update_time", "")),
        }

    def get_financials(self, code: str, statement_type: int = 4,
                       financial_type: str = "ANNUAL", num: int = 5) -> dict:
        """财务报表。statement_type: 1=利润表 2=资产负债表 3=现金流量表 4=关键指标。

        返回结构含 structure_list（字段定义）与 report_list（按期的 item_list），
        供财务 Agent 按 display_name 匹配指标，不做字段硬编码。
        """
        futu_code = self._normalise_code(code)
        data = self._call(
            "get_financials_statements", futu_code,
            statement_type=statement_type, financial_type=financial_type, num=num,
        )
        if not isinstance(data, dict):
            return {"available": False, "reason": "财务接口返回格式异常"}
        reports = data.get("report_list") or []
        return {
            "available": bool(reports),
            "statement_type": statement_type,
            "financial_type": financial_type,
            "structure_list": data.get("structure_list") or [],
            "reports": reports,
            "next_key": data.get("next_key"),
        }

    def get_valuation_detail(self, code: str, valuation_type: str = "PE",
                             interval_type: str = "YEAR_5") -> dict:
        """估值详情：含当前值、历史分位、均值、历史序列、行业分布。"""
        futu_code = self._normalise_code(code)
        data = self._call(
            "get_valuation_detail", futu_code,
            valuation_type=valuation_type, interval_type=interval_type,
        )
        if not isinstance(data, dict) or "trend" not in data:
            return {"available": False, "reason": "估值详情接口未返回 trend 数据"}
        return {"available": True, "valuation_type": valuation_type,
                "interval_type": interval_type, "detail": data}

    def get_capital_flow(self, code: str, period_type: str = "DAY", days: int = 10) -> dict:
        """个股资金流向。返回最近若干日的主力/超大/大/中/小单净流入。"""
        futu_code = self._normalise_code(code)
        end = datetime.now().date()
        start = end - timedelta(days=days * 2 + 10)
        data = self._call(
            "get_capital_flow", futu_code,
            period_type=period_type, start=start.isoformat(), end=end.isoformat(),
        )
        rows = data if isinstance(data, list) else []
        return {"available": bool(rows), "period_type": period_type,
                "rows": rows[-days:] if rows else [], "raw_count": len(rows)}

    def get_capital_distribution(self, code: str) -> dict:
        """个股资金分布：特大/大/中/小单的流入流出与更新时间。"""
        futu_code = self._normalise_code(code)
        data = self._call("get_capital_distribution", futu_code)
        if not isinstance(data, dict):
            return {"available": False, "reason": "资金分布接口返回格式异常"}
        return {"available": True, "distribution": data}

    def get_institutional_holdings(self, code: str, num: int = 10) -> dict:
        """机构持股明细。接口不可用时返回 available=False，不编造持仓。"""
        futu_code = self._normalise_code(code)
        try:
            data = self._call("get_shareholders_institutional", futu_code, num=num)
        except RuntimeError as exc:
            return {"available": False, "reason": str(exc)}
        if isinstance(data, dict):
            rows = data.get("institution_holder_list") or data.get("holder_list") or []
            return {"available": bool(rows), "rows": rows, "raw_keys": list(data.keys())}
        if isinstance(data, pd.DataFrame):
            return {"available": not data.empty, "rows": data.to_dict("records") if not data.empty else []}
        return {"available": False, "reason": "机构持股接口返回格式异常"}

    def get_news(self, keyword: str, max_count: int = 20,
                 news_sub_type: str = "ALL") -> dict:
        """资讯/公告检索。keyword 通常用股票名称，避免代码检索不到结果。"""
        try:
            data = self._call(
                "get_search_news", keyword,
                max_count=max_count, news_sub_type=news_sub_type,
            )
        except RuntimeError as exc:
            return {"available": False, "reason": str(exc)}
        rows = data if isinstance(data, list) else []
        return {"available": bool(rows), "keyword": keyword,
                "sub_type": news_sub_type, "rows": rows}

    def get_analyst_consensus(self, code: str) -> dict:
        """分析师评级概述，作为机构观点的补充，不等同于持仓。"""
        futu_code = self._normalise_code(code)
        try:
            data = self._call("get_research_analyst_consensus", futu_code)
        except RuntimeError as exc:
            return {"available": False, "reason": str(exc)}
        if isinstance(data, pd.DataFrame):
            return {"available": not data.empty,
                    "rows": data.to_dict("records") if not data.empty else []}
        if isinstance(data, dict):
            return {"available": bool(data), "data": data}
        return {"available": False, "reason": "评级接口返回格式异常"}
