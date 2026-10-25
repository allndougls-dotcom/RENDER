"""SIDI Growth: audit real historic scores and estimate time-to-15% (research only).

This is a reference-data study, NOT a validated multi-year point-in-time
fundamental backtest. Archived scores exist from 2026-08 onward. Prices must
come from daily OHLC observed after each historical signal.
"""
import csv, io, json, math, re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from recover_original_ingesta_history import git, col
from quality_75_correction_swing import prices

OUT=Path("results/quality_swing/sidi_growth_foundation")
HORIZONS=(20,40,60,120,252)
TARGET=1.15
def signals():
    found={};seen=set();report=[]
    commits=git("log","--all","--format=%H","--","data/master").decode().splitlines()
    for sha in commits:
        for path in git("ls-tree","-r","--name-only",sha,"--","data/master").decode().splitlines():
            match=re.fullmatch(r"data/master/sp500_full_export_([0-9]{8})[.]csv",path)
            if not match:continue
            blob=git("rev-parse",sha+":"+path).decode().strip()
            if blob in seen:continue
            seen.add(blob)
            dt=match.group(1);day=f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}"
            if day<"2026-08-01":continue
            try:
                reader=csv.DictReader(io.StringIO(git("show",sha+":"+path).decode("utf-8-sig")))
                fields=reader.fieldnames or []
                ticker=col(fields,("ticker","symbol","stock_symbol"))
                score=col(fields,("combined_score","score_total","total_score","sidi_score","score_final","final_score","score"))
                fund=col(fields,("fund_score","fundamental_score"))
                tech=col(fields,("tech_score","technical_score"))
                if not ticker or not score:
                    report.append({"date":day,"status":"missing_score_columns","columns":fields});continue
                count=0
                for row in reader:
                    symbol=(row.get(ticker) or "").strip().upper()
                    if not symbol:continue
                    try:value=float(row[score])
                    except (TypeError,ValueError):continue
                    if not math.isfinite(value) or not 0<=value<=10:continue
                    key=(day,symbol)
                    if key not in found:
                        found[key]={"signal_date":day,"ticker":symbol,"score":value,
                            "fund_score":row.get(fund,"") if fund else "",
                            "tech_score":row.get(tech,"") if tech else "",
                            "commit":sha[:12],"csv_path":path}
                    count+=1
                report.append({"date":day,"status":"parsed","rows":count,"blob":blob[:12]})
            except Exception as exc:
                report.append({"date":day,"status":"unreadable","reason":type(exc).__name__})
    return sorted(found.values(),key=lambda x:(x["ticker"],x["signal_date"])),report
def observed(signal,bars):
    indices=[i for i,p in enumerate(bars) if p["date"]>signal["signal_date"]]
    if not indices:return None
    idx=indices[0];entry=bars[idx]["open"]
    if entry<=0:return None
    sample=bars[idx:idx+max(HORIZONS)]
    hit=next((j+1 for j,p in enumerate(sample) if p["high"]>=entry*TARGET),None)
    lows=[p["low"] for p in sample]
    return {"entry_date":bars[idx]["date"],"entry_open":round(entry,4),
            "first_hit_sessions":hit,"observed_sessions":len(sample),
            "max_drawdown_pct":round(100*(min(lows)/entry-1),2) if lows else None}
def main():
    OUT.mkdir(parents=True,exist_ok=True)
    all_signals,coverage=signals()
    by_ticker=defaultdict(list)
    for x in all_signals:by_ticker[x["ticker"]].append(x)
    result=[];data_end=(date.today()+timedelta(days=1)).isoformat()
    for n,(ticker,rows) in enumerate(sorted(by_ticker.items()),1):
        bars=prices(ticker,"2026-07-01",data_end)
        for signal in rows:
            obs=observed(signal,bars)
            if not obs:continue
            result.append({**signal,**obs})
        if n%50==0:print("Processed",n,"of",len(by_ticker),flush=True)
    cols=("signal_date","ticker","score","fund_score","tech_score","entry_date",
          "entry_open","first_hit_sessions","observed_sessions","max_drawdown_pct","commit","csv_path")
    with (OUT/"observations.csv").open("w",newline="",encoding="utf-8") as fh:
        writer=csv.DictWriter(fh,fieldnames=cols);writer.writeheader();writer.writerows(result)
    matrix=[]
    for lower,upper in ((0,6.5),(6.5,7),(7,7.5),(7.5,8),(8,8.5),(8.5,10.01)):
        band=[x for x in result if lower<=x["score"]<upper]
        for horizon in HORIZONS:
            complete=[x for x in band if x["observed_sessions"]>=horizon or (x["first_hit_sessions"] is not None and x["first_hit_sessions"]<=horizon)]
            hits=[x for x in complete if x["first_hit_sessions"] is not None and x["first_hit_sessions"]<=horizon]
            matrix.append({"score_band":f"[{lower},{upper})","horizon_sessions":horizon,
                "signals_with_entry":len(band),"resolved_for_horizon":len(complete),
                "hits":len(hits),"observed_hit_pct":round(100*len(hits)/len(complete),2) if complete else None,
                "censored":len(band)-len(complete)})
    report={"signal_rows":len(all_signals),"observed_rows":len(result),
        "unique_tickers":len(by_ticker),"snapshots":coverage,"matrix":matrix,
        "caveats":["Repeated ticker-day signals are not independent trades",
          "Archived score snapshots begin August 2026; insufficient follow-up for long horizons",
          "Yahoo adjusted OHLC can be revised, and tickers that disappear are absent",
          "First target touch is not a realised investment return",
          "No validated multi-year historical SIDI scores or historical constituent universe yet"]}
    (OUT/"summary.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print("Archived ticker-days",len(all_signals),"observed",len(result),flush=True)
if __name__=="__main__":main()
