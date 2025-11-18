import pandas as pd

class SectorAgent:
    def run(self, sectors, top_n=10):
        df = sectors.copy()
        for c in ["change_pct", "up_count", "down_count", "amount", "leader_change_pct"]:
            if c not in df: df[c] = 0
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        total = (df.up_count + df.down_count).replace(0, pd.NA)
        df["breadth"] = (df.up_count / total).fillna(0)
        df["return_score"] = df.change_pct.rank(pct=True)
        df["breadth_score"] = df.breadth.rank(pct=True)
        df["amount_score"] = df.amount.rank(pct=True)
        df["leader_score"] = df.leader_change_pct.rank(pct=True)
        df["score"] = (df.return_score*.35 + df.breadth_score*.30 + df.amount_score*.15 + df.leader_score*.20)*100
        return df[df.change_pct > 0].sort_values("score", ascending=False).head(top_n).reset_index(drop=True)
