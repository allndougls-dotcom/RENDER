"""SIDI QUALITY SWING - experimento aislado, SIN scores retrospectivos.
Uso:
 python research/quality_swing.py --prices data/backtest_prices --snapshots data/master --out results/quality_swing
Los snapshots deben ser sp500_full_export_YYYYMMDD.csv con score point-in-time.
El archivo de precios por ticker incluye Date, Open, High, Low, Close.
"""
import argparse
import csv
import json
import math
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

VARIANTS = [(10,5,20),(10,7,40),(10,8,60),(15,5,30),(15,7,60),(15,10,90)]
DATE_RE = re.compile(r"sp500_full_export_(\d{8})\.csv$")
SCORE_GROUPS = ("exceptional","control")
MODES = ("quality_full","quality_flex")

def asfloat(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError):
        return None

def truth(v):
    return str(v).strip().lower() in {"true","1","yes","si"}

def load_snapshots(folder):
    by_ticker=defaultdict(list)
    files=0
    for p in sorted(Path(folder).glob("sp500_full_export_*.csv")):
        m=DATE_RE.fullmatch(p.name)
        if not m:continue
        day=date.fromisoformat(f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}")
        with p.open(encoding="utf-8-sig",newline="") as fh:
            for row in csv.DictReader(fh):
                ticker=(row.get("ticker") or "").strip().upper()
                score=asfloat(row.get("combined_score"))
                if not ticker or score is None:continue
                row["_asof"]=day; row["_score"]=score
                by_ticker[ticker].append(row)
        files+=1
    for records in by_ticker.values():
        records.sort(key=lambda x:x["_asof"])
    return by_ticker,files

def prices_for(path):
    with path.open(encoding="utf-8-sig",newline="") as fh:
        out=[]
        for x in csv.DictReader(fh):
            try:
                d=date.fromisoformat(x["Date"][:10])
                prices=[asfloat(x.get(k)) for k in ("Open","High","Low","Close")]
                if any(v is None or v<=0 for v in prices):continue
                out.append((d,*prices))
            except (ValueError,KeyError):continue
    return sorted(out)

def gate_mode(row,mode):
    if mode=="quality_full":return truth(row.get("full_setup"))
    dd=asfloat(row.get("drawdown_60d"))
    rsi=asfloat(row.get("rsi_14"))
    return dd is not None and dd>=12 and rsi is not None and rsi<45

def test_exit(bars,index,tp,sl,max_hold):
    entry=bars[index][1] # next available session OPEN
    end=min(index+max_hold-1,len(bars)-1)
    for j in range(index,end+1):
        day,op,high,low,close=bars[j]
        # Gap through stop / target at open must be filled at open.
        if op <=entry*(1-sl/100):return day,(op/entry-1)*100,"SL_GAP",j
        if op >=entry*(1+tp/100):return day,(op/entry-1)*100,"TP_GAP",j
        hit_sl=low<=entry*(1-sl/100)
        hit_tp=high>=entry*(1+tp/100)
        if hit_sl:return day,-sl,"SL" if not hit_tp else "BOTH_SL_FIRST",j
        if hit_tp:return day,tp,"TP",j
    return bars[end][0],(bars[end][4]/entry-1)*100,"TIME",end

def execute(snaps, prices_dir, out, cooldown_days=0):
    trades=[]
    coverage={"tickers_with_snapshots":len(snaps),"tickers_with_prices":0,"signals_checked":0,"signals_eligible":0}
    for ticker,records in snaps.items():
        p=Path(prices_dir)/f"{ticker}.csv"
        if not p.is_file():continue
        bars=prices_for(p)
        if len(bars)<30:continue
        coverage["tickers_with_prices"]+=1
        days=[x[0] for x in bars]
        idx_by_date={d:i for i,d in enumerate(days)}
        for row in records:
            signal_day=row["_asof"]
            # Never use data from an earlier future-dated daily file.
            # Snapshot dated D contains end-of-day info: earliest trade D+1.
            idx=next((i for i,d in enumerate(days) if d>signal_day),None)
            if idx is None:continue
            coverage["signals_checked"]+=1
            score=row["_score"]
            group="exceptional" if score>8.5 else "control" if 6.5<=score<=8.5 else None
            if group is None:continue
            for mode in MODES:
                if not gate_mode(row,mode):continue
                coverage["signals_eligible"]+=1
                for tp,sl,hold in VARIANTS:
                    exit_day,pct,reason,end_i=test_exit(bars,idx,tp,sl,hold)
                    trades.append(dict(ticker=ticker,signal_date=str(signal_day),entry_date=str(days[idx]),
                                       exit_date=str(exit_day),score=score,group=group,mode=mode,
                                       tp=tp,sl=sl,max_hold=hold,return_pct=round(pct,5),exit_reason=reason,
                                       bars_held=end_i-idx+1))
    # Avoid overlapping positions in same ticker/strategy and repeated daily snapshots.
    selected=[]
    by_config=defaultdict(list)
    for t in trades:
        by_config[(t["ticker"],t["group"],t["mode"],t["tp"],t["sl"],t["max_hold"])].append(t)
    for group in by_config.values():
        last_exit=date.min
        for t in sorted(group,key=lambda x:x["entry_date"]):
            entry=date.fromisoformat(t["entry_date"])
            if entry<=last_exit+timedelta(days=cooldown_days):continue
            selected.append(t)
            last_exit=date.fromisoformat(t["exit_date"])
    summary=[]
    config=defaultdict(list)
    for t in selected:config[(t["group"],t["mode"],t["tp"],t["sl"],t["max_hold"])].append(t)
    for (group,mode,tp,sl,hold),tr in sorted(config.items()):
        returns=[t["return_pct"] for t in tr]
        wins=[x for x in returns if x>0]
        losses=[x for x in returns if x<0]
        summary.append(dict(group=group,mode=mode,tp=tp,sl=sl,max_hold=hold,n=len(tr),
          win_rate_pct=round(100*len(wins)/len(tr),2),mean_return_pct=round(sum(returns)/len(tr),3),
          profit_factor=round(sum(wins)/abs(sum(losses)),3) if losses else None,
          tp_hit_pct=round(100*sum(t["exit_reason"].startswith("TP") for t in tr)/len(tr),2),
          mean_bars=round(sum(t["bars_held"] for t in tr)/len(tr),2)))
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    for name,rows in (("trades.csv",selected),("summary.csv",summary)):
        if rows:
            with (out/name).open("w",newline="",encoding="utf-8") as f:
                writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    coverage["trades"]=len(selected)
    coverage["limitations"]=["No portfolio sizing/costs in this exploratory study",
      "Historical ticker universe may suffer survivorship bias",
      "Snapshot must contain contemporaneous score, not backfilled current score",
      "Both stop and target in one candle: conservative stop first",
      "A snapshot's signal is traded at next available open"]
    (out/"coverage.json").write_text(json.dumps(coverage,indent=2),encoding="utf-8")
    print(json.dumps(coverage,indent=2))
    if not selected:print("NO VALID RESULTS: missing overlapping dated snapshots and price histories.")
    else:print("Wrote",out/"summary.csv")

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--prices",default="data/backtest_prices")
    ap.add_argument("--snapshots",default="data/master")
    ap.add_argument("--out",default="results/quality_swing")
    args=ap.parse_args()
    snapshots,files=load_snapshots(args.snapshots)
    print("Dated snapshots loaded:",files)
    execute(snapshots,args.prices,args.out)
