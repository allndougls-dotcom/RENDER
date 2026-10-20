"""Outcome study for ORIGINAL SIDI ingest scores, not a trading strategy.

Next-session adjusted open entry. Future adjusted daily high TP +10/+15.
Signals deduplicated ticker/date; independent episodes use non-overlapping
40-trading-session windows. Signals not mature are censored, not losers.
"""
import csv,json,math
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
import pandas as pd
import yfinance as yf
SOURCE=Path("results/quality_swing/original_ingesta_history/historical_scores_above_8.csv")
OUT=Path("results/quality_swing/original_ingesta_outcomes")
HORIZONS=(20,40,60,90)
def get_prices(ticker,start,end):
    df=yf.download(ticker,start=start,end=end,auto_adjust=True,actions=False,progress=False,threads=False)
    if df.empty:return []
    if isinstance(df.columns,pd.MultiIndex):df.columns=df.columns.get_level_values(0)
    return [(pd.Timestamp(idx).date().isoformat(),float(row["Open"]),float(row["High"]))
            for idx,row in df.iterrows()
            if pd.notna(row["Open"]) and pd.notna(row["High"]) and float(row["Open"])>0]
def records():
    with SOURCE.open(newline="",encoding="utf-8") as f:input_rows=list(csv.DictReader(f))
    grouped={}
    for r in input_rows:
        key=(r["ticker"],r["date"])
        if key not in grouped or float(r["combined_score"])>float(grouped[key]["combined_score"]):grouped[key]=r
    return sorted(grouped.values(),key=lambda r:(r["ticker"],r["date"]))
def evaluate(row,prices):
    future=[i for i,p in enumerate(prices) if p[0]>row["date"]]
    if not future:return []
    start=future[0]
    entry=prices[start][1]
    output=[]
    for horizon in HORIZONS:
        seq=prices[start:start+horizon]
        mature=len(seq)>=horizon
        peak=max((p[2] for p in seq),default=entry)
        tp10=any(p[2]>=entry*1.10 for p in seq)
        tp15=any(p[2]>=entry*1.15 for p in seq)
        output.append({"ticker":row["ticker"],"signal_date":row["date"],
                       "score":row["combined_score"],"entry_date":prices[start][0],
                       "entry_open_adj":round(entry,5),"horizon_sessions":horizon,
                       "sessions_observed":len(seq),"full_horizon":mature,
                       "max_favorable_pct":round(100*(peak/entry-1),2),
                       "hit_10":int(tp10),"hit_15":int(tp15)})
    return output
def summary(rows):
    data=[]
    for horizon in HORIZONS:
        subset=[x for x in rows if x["horizon_sessions"]==horizon]
        mature=[x for x in subset if x["full_horizon"]]
        for tp in ("hit_10","hit_15"):
            hits=sum(x[tp] for x in subset)
            hits_mature=sum(x[tp] for x in mature)
            data.append({"horizon":horizon,"target":tp,"signals_with_entry":len(subset),
                "full_horizon_signals":len(mature),"hits_observed":hits,
                "mature_hits":hits_mature,
                "mature_hit_rate_pct":round(100*hits_mature/len(mature),1) if mature else None,
                "unknown_or_censored":len(subset)-len(mature)})
    return data
def main():
    OUT.mkdir(parents=True,exist_ok=True)
    signals=records()
    price_map={}
    for ticker in sorted({r["ticker"] for r in signals}):
        print("Downloading adjusted OHLC for",ticker,flush=True)
        price_map[ticker]=get_prices(ticker,"2026-08-01","2026-10-22")
        print("Sessions available",len(price_map[ticker]),flush=True)
    rows=[]
    for signal in signals:rows.extend(evaluate(signal,price_map.get(signal["ticker"],[])))
    independent=[]
    # Keep the first original signal per ticker until 40 actual trading sessions
    # after its entry: prevents counting overlapping daily appearances as trades.
    for ticker in sorted(price_map):
        tx=sorted((x for x in rows if x["ticker"]==ticker and x["horizon_sessions"]==40),
                  key=lambda x:x["entry_date"])
        selected=[];last_index=-999
        idx_by_date={r[0]:i for i,r in enumerate(price_map[ticker])}
        for x in tx:
            ix=idx_by_date.get(x["entry_date"],-1)
            if ix<last_index+40:continue
            selected.append(x);last_index=ix
        starts={(x["ticker"],x["signal_date"]) for x in selected}
        independent.extend([x for x in rows if (x["ticker"],x["signal_date"]) in starts])
    fields=("ticker","signal_date","score","entry_date","entry_open_adj","horizon_sessions",
            "sessions_observed","full_horizon","max_favorable_pct","hit_10","hit_15")
    for name,data in (("daily_signals",rows),("independent_40_session_episodes",independent)):
        with (OUT/(name+".csv")).open("w",newline="",encoding="utf-8") as f:
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(data)
    report={"signal_dates":len(signals),"unique_tickers":len({r["ticker"] for r in signals}),
            "independent_episodes":len(independent)//len(HORIZONS),
            "available_sessions":{t:len(v) for t,v in price_map.items()},
            "daily_signals":summary(rows),"independent_episodes_results":summary(independent),
            "method":"Split-adjusted next session opening price; intraday adjusted high reaches target. No stops, costs, slippage or dividends (not an execution backtest). Incomplete horizons are censored; mature rate excludes incomplete observations. Overlapping daily signals are not independent.",
            "data_end":"2026-10-21 (exclusive 2026-10-22)"}
    (OUT/"summary.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2),flush=True)
if __name__=="__main__":main()
