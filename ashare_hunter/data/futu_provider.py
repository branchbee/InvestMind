from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import futu as ft

from ashare_hunter.utils.logging import log_df

logger = logging.getLogger(__name__)


def _summarise_args(args, kwargs) -> str:
    """参数摘要：长代码列表只打印数量与首尾，避免把 400 个代码刷进日志。"""
    parts = []
    for arg in args:
        if isinstance(arg, (list, tuple)):
            parts.append(f"list[{len(arg)}]={arg[0] if arg else '-'}..{arg[-1] if arg else '-'}")
        else:
            text = repr(arg)
            parts.append(text[:80] + "…" if len(text) > 80 else text)
    for key, value in kwargs.items():
        parts.append(f"{key}={repr(value)[:80]}")
    return ", ".join(parts)[:400]


def _summarise_data(data) -> str:
    """返回值摘要：规模 + 列名/键名，不打印具体数值。"""
    if isinstance(data, pd.DataFrame):
        return f"DataFrame{data.shape} cols={list(data.columns)[:8]}"
    if isinstance(data, dict):
        return f"dict(keys={list(data.keys())[:8]})"
    if isinstance(data, (list, tuple)):
        return f"{type(data).__name__}(len={len(data)})"
    return type(data).__name__


# 富途美股 get_stock_basicinfo 返回的 exchange_type 中，通常不随基础行情权限
# 提供正股快照的交易所类型。US_PINK 为 OTC 粉单/场外市场，是 get_market_snapshot
# 「整批失败：暂不提供美股 OTC 市场行情」的主因。
US_NO_QUOTE_EXCH = frozenset({
    "US_PINK",      # OTC 粉单 / 场外市场
    "US_OPTION",    # 美股期权
    "US_NYMEX", "US_COMEX", "US_CBOT", "US_CME", "US_CBOE",  # 期货交易所
    "CC_CRYPTO",    # 加密货币
})

# 快照请求单批上限（富途接口单次最多 400 个标的）
SNAPSHOT_BATCH_SIZE = 400


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
        us_exclude_otc: bool = True,
        snapshot_batch_size: int = SNAPSHOT_BATCH_SIZE,
    ):
        self.host = host
        self.port = int(port)
        self.cache_db = Path(cache_db)
        self.cache_db.parent.mkdir(parents=True, exist_ok=True)
        self.max_plate_refresh_per_run = int(max_plate_refresh_per_run)
        self.plate_cache_days = int(plate_cache_days)
        self.request_interval = max(float(request_interval), 0.0)
        # 美股 OTC 等无行情权限标的的源头过滤开关与快照分批大小
        self.us_exclude_otc = bool(us_exclude_otc)
        self.snapshot_batch_size = min(max(int(snapshot_batch_size), 1), SNAPSHOT_BATCH_SIZE)
        self._last_request_at = 0.0
        self._last_plate_request_at = 0.0
        self._quote_ctx = None
        self._spot_cache: Optional[pd.DataFrame] = None
        self._spot_market: str = "SH"
        self._ensure_db()
        logger.info(
            "FutuProvider 就绪：OpenD=%s:%s 缓存库=%s 快照批大小=%d OTC过滤=%s 请求间隔=%.2fs",
            self.host, self.port, self.cache_db, self.snapshot_batch_size,
            self.us_exclude_otc, self.request_interval,
        )

    @contextmanager
    def _db(self):
        """SQLite 连接上下文：提交事务并确保关闭连接。

        ``with sqlite3.connect(...)`` 只负责提交/回滚，不会关闭连接，
        长时间运行的 GUI 会累积未释放的连接并触发 ResourceWarning。
        """
        conn = sqlite3.connect(self.cache_db)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_db(self):
        with self._db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS plate_members (
                    plate_code TEXT NOT NULL,
                    plate_name TEXT NOT NULL,
                    board_type TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    stock_name TEXT,
                    market TEXT NOT NULL DEFAULT 'SH',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (plate_code, stock_code)
                )
            """)
            # 兼容旧库：为没有 market 列的历史缓存表补列（旧数据默认 A 股 SH）
            cols = {row[1] for row in conn.execute("PRAGMA table_info(plate_members)").fetchall()}
            if "market" not in cols:
                conn.execute("ALTER TABLE plate_members ADD COLUMN market TEXT NOT NULL DEFAULT 'SH'")
                logger.info("板块缓存旧库迁移：补充 market 列（历史数据标记为 SH）")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_plate_members_name
                ON plate_members(plate_name, board_type, market)
            """)
        logger.debug("板块缓存数据库就绪：%s", self.cache_db)

    def _ctx(self):
        if self._quote_ctx is None:
            logger.info("连接 Futu OpenD %s:%s", self.host, self.port)
            self._quote_ctx = ft.OpenQuoteContext(host=self.host, port=self.port)
        return self._quote_ctx

    def close(self):
        if self._quote_ctx is not None:
            logger.debug("关闭 Futu OpenD 连接")
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
        if wait > 0.05:
            logger.debug("接口限频等待 %.2fs", wait)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _pace_plate(self):
        # get_plate_list / get_plate_stock 官方限制均为每 30 秒最多 10 次。
        elapsed = time.monotonic() - self._last_plate_request_at
        wait = 3.1 - elapsed
        if wait > 0.05:
            logger.debug("板块接口限频等待 %.2fs", wait)
        if wait > 0:
            time.sleep(wait)
        self._last_plate_request_at = time.monotonic()

    def _call(self, method_name: str, *args, **kwargs):
        if method_name in {"get_plate_list", "get_plate_stock"}:
            self._pace_plate()
        else:
            self._pace()
        detail = _summarise_args(args, kwargs)
        logger.debug("Futu 请求 %s | 参数: %s", method_name, detail)
        started = time.perf_counter()
        try:
            method = getattr(self._ctx(), method_name)
            ret, data = method(*args, **kwargs)
        except Exception as exc:
            logger.error(
                "Futu %s 异常（%.3fs）：%s | 参数: %s",
                method_name, time.perf_counter() - started, exc, detail, exc_info=True,
            )
            raise
        elapsed = time.perf_counter() - started
        if ret != ft.RET_OK:
            # 错误信息里通常含触发失败的具体代码（如 OTC 标的），务必完整保留
            logger.error("Futu %s 返回失败（%.3fs）：%s | 参数: %s",
                         method_name, elapsed, data, detail)
            raise RuntimeError(f"Futu {method_name} 调用失败：{data}")
        logger.debug("Futu 响应 %s | 耗时 %.3fs | %s",
                     method_name, elapsed, _summarise_data(data))
        return data

    @staticmethod
    def _normalise_code(code: str) -> str:
        """把多种代码形态统一为 Futu 的 ``市场.代码`` 格式。

        支持 A 股（600519 / SH.600519 / 600519.SH）与美股
        （AAPL / US.AAPL / AAPL.US / BRK.B）。纯数字按 A 股规则处理，
        含字母则视为美股 ticker；已有市场前缀/后缀的保持对应市场。
        """
        value = str(code).strip()
        if not value:
            return value
        upper = value.upper()

        if "." in value:
            head, _, tail = value.partition(".")
            head_u = head.upper()
            if head_u in ("SH", "SZ", "US", "HK"):  # 前缀式优先：US.BRK.B / SH.600519
                return f"{head_u}.{value[len(head) + 1:].upper()}" if head_u == "US" else f"{head_u}.{value[len(head) + 1:]}"
            suffix = upper.rsplit(".", 1)[-1]
            if suffix in ("SH", "SZ", "US", "HK"):  # 后缀式：600519.SH / BRK.B.US
                stem = value.rsplit(".", 1)[0]
                if suffix in ("SH", "SZ") and stem.isdigit():
                    return f"{suffix}.{stem.zfill(6)}"
                return f"{suffix}.{stem.upper()}"
            return f"US.{upper}"  # 其他点号（如 BRK.B）按美股处理

        if value.isdigit():
            normalized = value.zfill(6)
            return f"SH.{normalized}" if normalized.startswith(("5", "6", "9")) else f"SZ.{normalized}"
        return f"US.{upper}"

    @staticmethod
    def _code6(code: str) -> str:
        """提取代码主体：A 股为 6 位数字，美股为完整 ticker（含层级如 BRK.B）。"""
        value = FutuProvider._normalise_code(code)
        if value.upper().startswith("US."):
            return value[3:]
        return value.rsplit(".", 1)[-1] if "." in value else value

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

    def _shape_quote(self, basic: pd.DataFrame, quote: pd.DataFrame) -> pd.DataFrame:
        """把 Futu 快照规整为各 Agent 使用的统一字段（A 股 / 美股共用）。

        涨跌幅一律由 last_price / prev_close_price 计算，缺失保留 NA，
        不把缺失值填成虚构数字；成交额字段名按 Futu 的 turnover 语义映射。
        """
        df = basic.merge(quote, on="code", how="inner", suffixes=("_basic", ""))
        if "name" not in df:
            df["name"] = df.get("name_basic", "")
        elif "name_basic" in df:
            df["name"] = df["name"].fillna(df["name_basic"])

        rename = {
            "turnover": "amount",
            "turnover_rate": "turnover",
            "last_price": "price",
            "volume": "volume",
        }
        for src, dst in rename.items():
            if src in df.columns:
                df[dst] = df[src]

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

        if "code6" not in df:
            df["code6"] = df["code"].map(self._code6)
        df["name"] = df["name"].fillna("").astype(str)
        # 以 code6/code 作为下游统一标识：A 股为 6 位数字，美股为 ticker
        df["code"] = df["code6"]
        return df

    def _fetch_snapshots(self, codes: list[str]) -> tuple[list[pd.DataFrame], list[str]]:
        """分批获取行情快照，自动隔离不提供行情的标的。

        富途 get_market_snapshot 是「整批语义」：一批内只要有一个代码不被提供
        行情（美股 OTC、退市、无权限等），服务端就对整批返回错误，而不是仅
        忽略那一个代码。因此单批失败时递归二分，定位并跳过坏代码，保留同批
        内其余可用标的的快照，避免个别标的拖垮整个市场扫描。
        """
        frames: list[pd.DataFrame] = []
        skipped: list[str] = []
        # 二分探测预算：极端情况下（整批全为无权限标的）避免请求数爆炸
        budget = [max(24, len(codes) // self.snapshot_batch_size * 12)]
        failures = [0]
        requests = [0]
        batches = (len(codes) + self.snapshot_batch_size - 1) // self.snapshot_batch_size
        logger.info("开始获取快照：标的 %d 个，批大小 %d，计划 %d 批",
                    len(codes), self.snapshot_batch_size, batches)

        def fetch(chunk: list[str]):
            if not chunk:
                return
            try:
                requests[0] += 1
                snap = self._call("get_market_snapshot", list(chunk))
                if snap is not None and not snap.empty:
                    frames.append(snap)
                    logger.debug("快照批成功 size=%d rows=%d", len(chunk), len(snap))
                return
            except Exception as exc:
                reason = str(exc)
                logger.debug("快照批失败 size=%d 原因=%s", len(chunk), reason)

            failures[0] += 1
            # 连续失败且一无所获：多半是 OpenD 断连 / 整体无权限，快速失败
            if not frames and failures[0] >= 15:
                logger.error("快照连续 %d 次失败且无有效数据，终止扫描", failures[0])
                raise RuntimeError(
                    "行情快照连续失败，请检查 Futu OpenD 连接与行情权限：" + reason
                )
            if len(chunk) == 1:
                skipped.append(f"{chunk[0]}（{reason}）")
                logger.debug("跳过无行情权限标的：%s", chunk[0])
                return
            if budget[0] <= 0:
                skipped.append(f"{len(chunk)} 个标的整批跳过（{reason}）")
                logger.warning("二分探测预算耗尽，整批跳过 %d 个标的", len(chunk))
                return
            budget[0] -= 1
            mid = len(chunk) // 2
            logger.debug("二分隔离坏标的：%d -> %d + %d", len(chunk), mid, len(chunk) - mid)
            fetch(chunk[:mid])
            fetch(chunk[mid:])

        for start in range(0, len(codes), self.snapshot_batch_size):
            fetch(codes[start:start + self.snapshot_batch_size])
        logger.info("快照获取完成：实际请求 %d 次，成功 %d 帧，跳过 %d 个标的",
                    requests[0], len(frames), len(skipped))
        return frames, skipped

    def get_spot(self) -> pd.DataFrame:
        """获取沪深 A 股快照；分批请求并自动跳过个别无行情标的。"""
        started = time.perf_counter()
        logger.info("开始获取 A 股全市场快照")
        basic = self._get_all_a_share_basic()
        logger.debug("A 股静态信息 %d 条", len(basic))
        codes = basic["code"].astype(str).tolist()
        frames, skipped = self._fetch_snapshots(codes)
        if skipped:
            logger.warning("A 股快照跳过 %d 个无行情标的：%s", len(skipped), skipped[:3])
        if not frames:
            logger.error("A 股快照全部失败，耗时 %.3fs", time.perf_counter() - started)
            raise RuntimeError("Futu 行情快照为空，请确认账号有 A 股行情权限。")

        quote = pd.concat(frames, ignore_index=True)
        df = self._shape_quote(basic, quote)
        self._spot_cache = df.copy()
        self._spot_market = "SH"
        logger.info("A 股快照完成：%d 条，跳过 %d 个，耗时 %.3fs",
                    len(df), len(skipped), time.perf_counter() - started)
        log_df(logger, "A股快照", df)
        return df

    def _get_all_us_stocks(self) -> pd.DataFrame:
        """获取美股全部正股静态信息，剔除 OTC 等无行情权限标的。"""
        data = self._call("get_stock_basicinfo", ft.Market.US, stock_type=ft.SecurityType.STOCK)
        if data is None or data.empty:
            raise RuntimeError("Futu 未返回美股股票静态信息，请检查 OpenD 连接和美股行情权限。")
        basic = data.copy()
        basic = basic[basic["code"].astype(str).str.startswith("US.")].drop_duplicates("code")
        if "name" not in basic:
            basic["name"] = ""
        logger.info("美股静态信息：US. 代码 %d 个", len(basic))

        # 全市场 basicinfo 含 OTC（US_PINK）等标的；账号通常无此类行情权限，
        # 混入快照批次会让 get_market_snapshot 整批失败。从源头剔除，
        # 兼容旧版 SDK 未返回 exchange_type 的情况（此时交由批次容错兜底）。
        excluded = 0
        if self.us_exclude_otc and "exchange_type" in basic.columns:
            exch = basic["exchange_type"].astype(str).str.strip().str.upper()
            keep = ~exch.isin(US_NO_QUOTE_EXCH)
            excluded = int((~keep).sum())
            if excluded:
                breakdown = exch[~keep].value_counts().to_dict()
                logger.info("源头过滤 %d 个无行情权限标的：%s", excluded, breakdown)
            else:
                logger.debug("未发现 OTC/无行情权限标的")
            basic = basic[keep].copy()
        else:
            logger.debug("未启用 OTC 过滤或缺少 exchange_type 列，交由批次容错兜底")
        logger.debug("可请求快照的美股代码 %d 个", len(basic))

        basic["code6"] = basic["code"].map(self._code6)
        basic = basic.reset_index(drop=True)
        basic.attrs["otc_excluded"] = excluded
        return basic

    def get_us_spot(self, symbols: Optional[list[str]] = None) -> pd.DataFrame:
        """获取美股全市场快照，输出字段与 A 股 get_spot 一致。

        默认扫描全部美股（用于板块聚合）。symbols 参数仅保留给离线测试/调试
        限定代码范围，生产路径不再依赖观察池。
        """
        started = time.perf_counter()
        skipped: list[str] = []
        otc_excluded = 0
        logger.info("开始获取美股快照：范围=%s", "限定代码" if symbols else "全市场")
        if symbols:  # 仅测试/调试用途
            codes = [self._normalise_code(s) for s in symbols]
            frames, skipped = self._fetch_snapshots(codes)
            if not frames:
                return pd.DataFrame(columns=["code", "name", "price", "change_pct", "amount", "turnover", "volume_ratio"])
            quote = pd.concat(frames, ignore_index=True).drop_duplicates("code")
            basic = quote[["code"]].copy()
            basic["name"] = quote["name"] if "name" in quote else ""
            basic["code6"] = basic["code"].map(self._code6)
            df = self._shape_quote(basic, quote)
        else:
            basic = self._get_all_us_stocks()
            otc_excluded = int(basic.attrs.get("otc_excluded", 0) or 0)
            codes = basic["code"].astype(str).tolist()
            if not codes:
                logger.error("源头过滤后没有可用的美股代码")
                raise RuntimeError(
                    "美股全市场静态信息中没有可取行情的正股（OTC 等无权限标的已过滤）。"
                    "请检查美股行情权限，或设置 US_EXCLUDE_OTC=false 关闭源头过滤。"
                )
            frames, skipped = self._fetch_snapshots(codes)
            if not frames:
                logger.error("美股快照全部失败，耗时 %.3fs，跳过标的 %d 个",
                             time.perf_counter() - started, len(skipped))
                raise RuntimeError(
                    "Futu 美股快照为空：请确认 OpenD 已连接、账号具备美股行情权限"
                    "（OTC 粉单市场行情通常需单独开通）。"
                    + (f" 本次另有 {len(skipped)} 个标的无行情权限被跳过。" if skipped else "")
                )
            if skipped:
                logger.warning("美股快照跳过 %d 个无行情标的：%s", len(skipped), skipped[:3])
            quote = pd.concat(frames, ignore_index=True).drop_duplicates("code")
            df = self._shape_quote(basic, quote)

        df["market"] = "US"
        # 容错统计通过 attrs 回传编排层：不改变列结构与函数签名，兼容离线测试
        df.attrs["snapshot_skipped"] = skipped
        df.attrs["otc_excluded"] = otc_excluded
        self._spot_cache = df.copy()
        self._spot_market = "US"
        logger.info("美股快照完成：%d 条，OTC 过滤 %d 个，无权限跳过 %d 个，耗时 %.3fs",
                    len(df), otc_excluded, len(skipped), time.perf_counter() - started)
        log_df(logger, "美股快照", df)
        return df

    def _get_plate_list(self, board_type: str, market: str = "SH") -> pd.DataFrame:
        plate_class = ft.Plate.INDUSTRY if board_type == "industry" else ft.Plate.CONCEPT
        # A 股文档说明板块列表不区分 SH/SZ，传任一市场即可返回沪深子板块。
        # 美股传 Market.US 返回 US.BKxxxx 形式的行业/概念板块。
        ft_market = ft.Market.US if market == "US" else ft.Market.SH
        data = self._call("get_plate_list", ft_market, plate_class)
        if data is None or data.empty:
            return pd.DataFrame(columns=["code", "plate_name", "board_type"])
        data = data.copy()
        data["board_type"] = board_type
        return data

    def _cached_plate_codes(self, market: str = "SH") -> set[str]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT plate_code FROM plate_members WHERE market = ?", (market,)
            ).fetchall()
        return {r[0] for r in rows}

    def _refresh_plate_members(self, plate: dict, board_type: str, market: str = "SH"):
        plate_code = str(plate["code"])
        plate_name = str(plate["plate_name"])
        members = self._call("get_plate_stock", plate_code)
        if members is None:
            members = pd.DataFrame()
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        prefixes = ("US.",) if market == "US" else ("SH.", "SZ.")
        if not members.empty:
            for _, row in members.iterrows():
                code = str(row.get("code", ""))
                if not code.startswith(prefixes):
                    continue
                rows.append((
                    plate_code, plate_name, board_type, code,
                    str(row.get("stock_name", "")), market, now
                ))
        with self._db() as conn:
            conn.execute("DELETE FROM plate_members WHERE plate_code = ?", (plate_code,))
            if rows:
                conn.executemany("""
                    INSERT OR REPLACE INTO plate_members
                    (plate_code, plate_name, board_type, stock_code, stock_name, market, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, rows)
        logger.debug("板块成分股刷新 %s(%s/%s)：接口返回 %d 条，写入缓存 %d 条",
                     plate_name, plate_code, market, len(members), len(rows))

    def _load_members(self, plate_code: str) -> pd.DataFrame:
        with self._db() as conn:
            df = pd.read_sql_query(
                "SELECT stock_code AS code, stock_name AS name FROM plate_members WHERE plate_code = ?",
                conn, params=(plate_code,)
            )
        return df

    def get_sectors(self, spot: Optional[pd.DataFrame] = None, market: str = "SH") -> pd.DataFrame:
        """
        获取板块列表，并通过板块成分股的当日快照聚合板块表现。
        首次运行只刷新 max_plate_refresh_per_run 个板块；后续运行逐步补齐。
        设置 FUTU_MAX_PLATE_REFRESH_PER_RUN=0 可尝试全量刷新（耗时较长）。
        market='US' 时扫描美股行业/概念板块，缓存与 A 股隔离。
        """
        started = time.perf_counter()
        spot = spot if spot is not None else self._spot_cache
        if spot is None or spot.empty:
            logger.error("计算板块强度时缺少行情快照")
            raise RuntimeError("请先调用 get_spot() / get_us_spot() 获取行情快照，再计算板块强度。")
        logger.info("开始计算 %s 板块强度：快照 %d 条", market, len(spot))

        plate_frames = [
            self._get_plate_list("industry", market),
            self._get_plate_list("concept", market)
        ]
        plates = pd.concat(plate_frames, ignore_index=True)
        logger.info("%s 板块列表：行业 %d 个 / 概念 %d 个，合计 %d 个",
                    market, len(plate_frames[0]), len(plate_frames[1]), len(plates))
        if plates.empty:
            logger.error("%s 板块列表为空，可能是账号缺少板块数据权限", market)
            raise RuntimeError(f"Futu 未返回 {market} 市场板块列表，请确认账号板块数据权限。")

        cached_codes = self._cached_plate_codes(market)
        logger.debug("%s 已有成分股缓存的板块 %d 个", market, len(cached_codes))
        now = datetime.now()
        refresh_candidates = []
        for _, p in plates.iterrows():
            code = str(p["code"])
            if code not in cached_codes:
                refresh_candidates.append(p.to_dict())
                continue
            # 缓存超过配置天数时进入增量刷新队列
            with self._db() as conn:
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
        logger.info("%s 板块缓存：待刷新 %d 个，本次刷新 %d 个（单次上限 %s）",
                    market, len(refresh_candidates), len(selected),
                    "不限" if limit <= 0 else limit)
        for plate in selected:
            try:
                self._refresh_plate_members(plate, plate["board_type"], market)
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
            logger.error("%s 板块聚合结果为空：缓存 %d 个板块均无法与快照匹配", market, len(plates))
            raise RuntimeError(
                "尚无可用板块成分股缓存。请增加 FUTU_MAX_PLATE_REFRESH_PER_RUN，"
                "或设置为 0 执行全量初始化（受 Futu 接口频率限制，可能耗时较长）。"
            )
        sectors = pd.DataFrame(results)
        logger.info("%s 板块聚合完成：有效板块 %d 个，耗时 %.3fs",
                    market, len(sectors), time.perf_counter() - started)
        log_df(logger, f"{market}板块强度", sectors)
        return sectors

    def get_sector_stocks(self, name: str, kind: str, market: str = "SH") -> pd.DataFrame:
        """取板块成分股并用当前市场快照补充行情。market 用于隔离 A股/美股同名板块。"""
        with self._db() as conn:
            rows = pd.read_sql_query(
                """SELECT DISTINCT stock_code AS futu_code, stock_name AS name
                   FROM plate_members WHERE plate_name = ? AND board_type = ? AND market = ?""",
                conn, params=(name, kind, market)
            )
        logger.debug("板块成分股 %s(%s/%s)：缓存 %d 条", name, kind, market, len(rows))
        if rows.empty:
            logger.error("板块 %s 无本地成分股缓存", name)
            raise RuntimeError(f"板块 {name} 暂无本地成分股缓存，请先运行板块缓存初始化。")

        rows["code"] = rows["futu_code"].map(self._code6)
        expected_market = "US" if market == "US" else "SH"
        if self._spot_cache is None or self._spot_market != expected_market:
            logger.debug("板块选股 %s：快照缓存未命中，重新获取 %s 快照", name, expected_market)
            spot = self.get_us_spot() if market == "US" else self.get_spot()
        else:
            logger.debug("板块选股 %s：复用 %s 快照缓存", name, expected_market)
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
        logger.debug("请求日线 %s：%s ~ %s，days=%d", futu_code, start, end, days)
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
            logger.debug("日线 %s 返回空数据", futu_code)
            return pd.DataFrame(columns=["date", "close", "pct_change"])
        hist = pd.concat(frames, ignore_index=True).drop_duplicates("time_key")
        hist = hist.rename(columns={"time_key": "date", "change_rate": "pct_change"})
        for col in ("close", "pct_change"):
            if col in hist:
                hist[col] = pd.to_numeric(hist[col], errors="coerce")
        logger.debug("日线 %s 完成：%d 页 / %d 行", futu_code, len(frames), len(hist))
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
            logger.debug("单股快照 %s 为空，估值维度将标记数据不足", futu_code)
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
