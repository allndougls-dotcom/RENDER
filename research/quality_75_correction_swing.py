"""Exploratory quality-correction swing study using original Git ingest scores.

PIT scores read from historical Git blobs; correction computed using price history
strictly preceding the signal. No production strategy code is modified.
"""
import csv,io,json,math,re
from collections import defaultdict
from datetime import date,timedelta
from pathlib import Path
import pandas as pd
import yfinance as yf
from recover_original_ingesta_history import git,col

OUT=Path("results/quality_swing/quality_75_correction")
HORIZONS=(40,60,90)
SCORES=(7.25,7.75)
def snapshots():
    found={}; commits=git("log","--all","--format=%H","--","data/master").decode().splitlines()
    for sha in commits:
        paths=git("ls-tree","-r","--name-only",sha,"--","data/master").decode().splitlines()
        for path in paths:
            match=re.fullmatch(r"data/master/sp500_full_export_([0-9]{8})[.]csv",path)
            if not match:continue
            dt=match.group(1);day=f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}"
            if day<"2026-08-01":continue
            blob=git("rev-parse",sha+":"+path).decode().strip()
            try:reader=csv.DictReader(io.StringIO(git("show",sha+":"+path).decode("utf-8-sig")))
            except (UnicodeError,Exception):continue
            names=reader.fieldnames or []
            ticker=col(names,("ticker","symbol","stock_symbol"))
            score=col(names,("combined_score","score_total","total_score","sidi_score","score_final","final_score","score"))
            if not ticker or not score:continue
            # Multiple ingest versions of one day: retain the earliest git commit encountered
            # as a snapshot variant, but avoid overweighting duplicate ticker/day.
            for row in reader:
                try:value=float(row[score])
                except (ValueError,TypeError):continue
                symbol=(row.get(ticker) or "").strip().upper()
                if not symbol or not math.isfinite(value):continue
                key=(symbol,day)
                if key not in found:found[key]={"ticker":symbol,"date":day,"score":value,"blob":blob[:12]}
    return list(found.values())
def prices(ticker,begin,end):
    try:
        df=yf.download(ticker,start=begin,end=end,auto_adjust=True,progress=False,threads=False)
        if df.empty:return []
        if isinstance(df.columns,pd.MultiIndex):df.columns=df.columns.get_level_values(0)
        return [{"date":pd.Timestamp(index).strftime("%Y-%m-%d"),
                 "open":float(row["Open"]),"high":float(row["High"]),
                 "low":float(row["Low"]),"close":float(row["Close"])}
                for index,row in df.iterrows() if all(pd.notna(row[k]) for k in ("Open","High","Low","Close"))]
    except Exception as exc:
        print("Missing price history",ticker,str(exc)[:90],flush=True)
        return []
def run():
    OUT.mkdir(parents=True,exist_ok=True)
    observations=snapshots()
    candidates=[x for x in observations if 7.0<=x["score"]<8.25]
    end=(date.today()+timedelta(days=1)).isoformat()
    index=defaultdict(list)
    for r in candidates:index[r["ticker"]].append(r)
    all_rows=[]
    for count,(ticker,signals) in enumerate(sorted(index.items()),1):
        if count%30==0:print("Processed",count,"of",len(index),flush=True)
        px=prices(ticker,"2026-03-01",end)
        if not px:continue
        for signal in signals:
            prior=[p for p in px if p["date"]<=signal["date"]]
            after=[p for p in px if p["date"]>signal["date"]]
            if len(prior)<60 or not after:continue
            last60=prior[-60:];highest=max(p["high"] for p in last60)
            drawdown=100*(prior[-1]["close"]/highest-1)
            if drawdown>-10:continue
            entry=after[0]["open"]
            for h in HORIZONS:
                window=after[:h]
                best=max(p["high"] for p in window)/entry-1
                worst=min(p["low"] for p in window)/entry-1
                all_rows.append({"ticker":ticker,"signal_date":signal["date"],"score":signal["score"],
                    "drawdown_60_pct":round(drawdown,2),"entry_date":after[0]["date"],
                    "entry_open":round(entry,4),"horizon":h,"observed_sessions":len(window),
                    "mature":int(len(window)>=h),"max_up_pct":round(best*100,2),
                    "max_down_pct":round(worst*100,2),"tp10":int(best>=.10),"tp15":int(best>=.15),
                    "bucket":"score_7_25_7_75" if 7.25<=signal["score"]<7.75 else
                    ("score_7_75_8_25" if 7.75<=signal["score"]<8.25 else "score_7_0_7_25"),
                    "correction_band":"deep_20plus" if drawdown<=-20 else
                        ("medium_15_20" if drawdown<=-15 else "shallow_10_15")})
    columns=("ticker","signal_date","score","drawdown_60_pct","entry_date","entry_open",
             "horizon","observed_sessions","mature","max_up_pct","max_down_pct",
             "tp10","tp15","bucket","correction_band")
    with (OUT/"signals.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=columns);w.writeheader();w.writerows(all_rows)
    # One entry per ticker per 40 actual price bars: de-correlate overlapping signals.
    independent=[];grouped=defaultdict(list)
    for r in all_rows:grouped[r["ticker"]].append(r)
    for ticker,rows in grouped.items():
        starts=sorted((r for r in rows if r["horizon"]==40),key=lambda r:r["entry_date"])
        selected=[];last=None
        for r in starts:
            d=date.fromisoformat(r["entry_date"])
            if last and (d-last).days<56:continue
            selected.append((r["ticker"],r["signal_date"]));last=d
        allowed=set(selected)
        independent.extend(r for r in rows if (r["ticker"],r["signal_date"]) in allowed)
    with (OUT/"nonoverlapping_episodes.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=columns);w.writeheader();w.writerows(independent)
    stats=[]
    for h in HORIZONS:
        for b in ("score_7_0_7_25","score_7_25_7_75","score_7_75_8_25"):
            for c in ("shallow_10_15","medium_15_20","deep_20plus"):
                sample=[x for x in independent if x["horizon"]==h and x["bucket"]==b and x["correction_band"]==c]
                complete=[x for x in sample if x["mature"]]
                stats.append({"horizon":h,"score_band":b,"correction":c,
                    "episodes":len(sample),"mature":len(complete),
                    "observed_tp10":sum(x["tp10"] for x in sample),
                    "observed_tp15":sum(x["tp15"] for x in sample),
                    "mature_tp10_rate_pct":round(100*sum(x["tp10"] for x in complete)/len(complete),1) if complete else None,
                    "mature_tp15_rate_pct":round(100*sum(x["tp15"] for x in complete)/len(complete),1) if complete else None})
    summary={"source_snapshots":len(observations),"price_download_tickers":len(index),
             "unique_independent_entries":len(independent)//len(HORIZONS),
             "matrix":stats,
             "limitations":"EXPLORATORY: current price data provider; surviving names, incomplete horizons, adjusted OHLC; no same-bar stop/target ordering, costs or dividends. No valid strategy WR until PIT universe and execution checks.",
             "signal_time":"CSV dated signal; 60 previous trading bars determine correction; next session adjusted open entry."}
    (OUT/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    print(json.dumps({"source_snapshots":len(observations),"entries":summary["unique_independent_entries"],"mature40":sum(x["mature"] for x in independent if x["horizon"]==40)},indent=2))
if __name__=="__main__":run()
