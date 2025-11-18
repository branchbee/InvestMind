import pandas as pd

class TechnicalAgent:
    def enrich(self, stocks, provider):
        if stocks.empty: return stocks
        rows = []
        for _, row in stocks.iterrows():
            item = row.to_dict()
            try:
                hist = provider.get_daily_history(row.code)
                close = hist.close.dropna() if not hist.empty and "close" in hist else pd.Series(dtype=float)
                item["ma5"] = float(close.tail(5).mean()) if len(close) >= 5 else None
                item["ma20"] = float(close.tail(20).mean()) if len(close) >= 20 else None
                item["return_5d_pct"] = float((close.iloc[-1]/close.iloc[-6]-1)*100) if len(close) >= 6 and close.iloc[-6] else None
                item["above_ma5"] = bool(close.iloc[-1] > close.tail(5).mean()) if len(close) >= 5 else None
                item["above_ma20"] = bool(close.iloc[-1] > close.tail(20).mean()) if len(close) >= 20 else None
            except Exception as exc:
                item["history_status"] = str(exc)
            rows.append(item)
        return pd.DataFrame(rows)
