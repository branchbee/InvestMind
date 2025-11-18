import logging
import numpy as np
import pandas as pd
logger = logging.getLogger(__name__)

class StockAgent:
    def run(self, sectors, provider, top_n=30, min_amount=50_000_000):
        chunks = []
        for _, s in sectors.iterrows():
            try:
                part = provider.get_sector_stocks(str(s.sector_name), str(s.board_type))
                part["sector_name"] = str(s.sector_name)
                part["sector_score"] = float(s.score)
                chunks.append(part)
            except Exception as exc:
                logger.warning("板块成分股获取失败 %s: %s", s.sector_name, exc)
        if not chunks: return pd.DataFrame()
        df = pd.concat(chunks, ignore_index=True, sort=False)
        for c in ["change_pct", "amount", "turnover", "volume_ratio"]:
            if c not in df: df[c] = 0
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        if "name" not in df:
            df["name"] = ""
        df["name"] = df["name"].fillna("").astype(str)
        df = df[~df.name.str.contains(r"ST|退", case=False, na=False)]
        df = df[(df.change_pct > 0) & (df.amount >= min_amount)]
        if df.empty: return df
        df["sector_names"] = df.groupby("code").sector_name.transform(lambda x: ",".join(sorted(set(x))))
        df["sector_score"] = df.groupby("code").sector_score.transform("max")
        df = df.drop_duplicates("code").copy()
        df["return_score"] = df.change_pct.rank(pct=True)
        df["amount_score"] = np.log1p(df.amount).rank(pct=True)
        df["turnover_score"] = df.turnover.rank(pct=True)
        df["volume_ratio_score"] = df.volume_ratio.rank(pct=True)
        df["sector_score_norm"] = df.sector_score / 100
        df["score"] = (df.return_score*.25 + df.amount_score*.15 + df.turnover_score*.10 +
                       df.volume_ratio_score*.15 + df.sector_score_norm*.35)*100
        return df.sort_values("score", ascending=False).head(top_n).reset_index(drop=True)
