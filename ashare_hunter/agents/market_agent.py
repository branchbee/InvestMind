class MarketAgent:
    def run(self, spot):
        df = spot
        up = int((df.change_pct > 0).sum())
        down = int((df.change_pct < 0).sum())
        flat = int((df.change_pct == 0).sum())
        breadth = up / max(up + down, 1)
        avg = float(df.change_pct.mean())
        regime = "偏强" if breadth >= .60 and avg > .3 else ("偏弱" if breadth <= .40 and avg < -.3 else "震荡")
        return {"market_regime": regime, "up_count": up, "down_count": down,
                "flat_count": flat, "advance_decline_ratio": round(breadth, 4),
                "avg_change_pct": round(avg, 3), "total_amount_yuan": round(float(df.amount.fillna(0).sum()), 2)}
