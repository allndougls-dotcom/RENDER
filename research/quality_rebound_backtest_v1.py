"""SIDI QUALITY REBOUND V1 — exploratory point-in-time signal backtest.

Uses archived SIDI scores, pre-signal correction and optional technical confirmation.
No modifications to FULL SIDI / WHITE BIRD or live trading.
"""
import argparse,csv,json,math
from datetime import date,timedelta
from collections import defaultdict
from pathlib import Path
import pandas as pd,yfinance as yf
from quality_75_correction_swing import snapshots,prices
OUT=Path("results/quality_swing/quality_rebound_v1")
HORIZONS=(40,60,90)
TPS=(10,15,20)
def sma(values,n):
 return sum(values[-n:])/n if len(values)>=n else None
def evaluate_bars(bars,start,horizon,tp,stop_pct=7):
 entry=bars[start]["open"]
 target=entry*(1+tp/100);stop=entry*(1-stop_pct/100)
 sample=bars[start:start+horizon]
 for i,b in enumerate(sample):
  # Gaps fill at the opening print. For stop + target both crossed inside one bar,
  # conservatively assume stop first.
  if b["open"]<=stop:return "SL",100*(b["open"]/entry-1),i+1
  if b["open"]>=target:return "TP",100*(b["open"]/entry-1),i+1
  if b["low"]<=stop:return "SL",-stop_pct,i+1
  if b["high"]>=target:return "TP",tp,i+1
 if len(sample)<horizon:return "CENSORED",None,len(sample)
 return "TIME",100*(sample[-1]["close"]/entry-1),len(sample)
def main():
 p=argparse.ArgumentParser();p.add_argument("--min-score",type=float,default=7.0)
 p.add_argument("--max-score",type=float,default=8.0)
 p.add_argument("--min-correction",type=float,default=15.0)
 args=p.parse_args()
 OUT.mkdir(parents=True,exist_ok=True)
 obs=[x for x in snapshots() if args.min_score<=x["score"]<args.max_score]
 symbols=sorted({x["ticker"] for x in obs})
 cutoff=(date.today()+timedelta(days=1)).isoformat()
 results=[];coverage=[]
 for idx,ticker in enumerate(symbols,1):
  bars=prices(ticker,"2026-03-01",cutoff)
  coverage.append({"ticker":ticker,"bars":len(bars)})
  if not bars:continue
  for signal in (x for x in obs if x["ticker"]==ticker):
   prior=[i for i,b in enumerate(bars) if b["date"]<=signal["date"]]
   if len(prior)<60:continue
   last=prior[-1]
   high60=max(bars[i]["high"] for i in prior[-60:])
   correction=100*(bars[last]["close"]/high60-1)
   if correction>-args.min_correction:continue
   # Confirmation calculated from prices available at the signal close.
   close=[bars[i]["close"] for i in prior]
   ma10=sma(close,10);ma20=sma(close,20)
   confirm=(ma10 is not None and ma20 is not None and
            close[-1]>ma10 and close[-1]>close[-2] and ma10>=ma20)
   immediate=last+1
   if immediate>=len(bars):continue
   for mode in ("immediate","confirmed"):
    if mode=="confirmed" and not confirm:continue
    for horizon in HORIZONS:
     for tp in TPS:
      status,pct,sessions=evaluate_bars(bars,immediate,horizon,tp)
      results.append({"ticker":ticker,"signal_date":signal["date"],"score":signal["score"],
       "correction_pct":round(correction,2),"entry_date":bars[immediate]["date"],"entry_open":round(bars[immediate]["open"],4),
       "mode":mode,"tp_pct":tp,"stop_pct":7,"horizon":horizon,"outcome":status,
       "gross_return_pct":round(pct,3) if pct is not None else "",
       "sessions":sessions})
  if idx%40==0:print("Evaluated",idx,"of",len(symbols),"symbols",flush=True)
 # Remove repeated daily observations by keeping first entry per ticker and 40-session block.
 nonoverlap=[];groups=defaultdict(list)
 for r in results:groups[(r["ticker"],r["mode"],r["horizon"],r["tp_pct"])].append(r)
 for key,rs in groups.items():
  rs.sort(key=lambda x:(x["entry_date"],x["signal_date"]))
  last=None
  for r in rs:
   d=date.fromisoformat(r["entry_date"])
   if last is not None and (d-last).days<56:continue
   nonoverlap.append(r);last=d
 fields=("ticker","signal_date","score","correction_pct","entry_date","entry_open",
         "mode","tp_pct","stop_pct","horizon","outcome","gross_return_pct","sessions")
 with (OUT/"episodes.csv").open("w",newline="",encoding="utf8") as fh:
  writer=csv.DictWriter(fh,fieldnames=fields);writer.writeheader();writer.writerows(nonoverlap)
 report=[]
 for mode in ("immediate","confirmed"):
  for horizon in HORIZONS:
   for tp in TPS:
    rs=[r for r in nonoverlap if r["mode"]==mode and r["horizon"]==horizon and r["tp_pct"]==tp]
    complete=[r for r in rs if r["outcome"]!="CENSORED"]
    report.append({"mode":mode,"sessions":horizon,"target_pct":tp,"entries":len(rs),
                   "resolved":len(complete),"censored":len(rs)-len(complete),
                   "tp":sum(r["outcome"]=="TP" for r in complete),
                   "stops":sum(r["outcome"]=="SL" for r in complete),
                   "timed":sum(r["outcome"]=="TIME" for r in complete),
                   "win_rate_resolved_pct":round(100*sum(r["outcome"]=="TP" for r in complete)/len(complete),1) if complete else None,
                   "mean_gross_return_resolved_pct":round(sum(float(r["gross_return_pct"]) for r in complete)/len(complete),2) if complete else None})
 summary={"signals_in_score_range":len(obs),"tickers_queried":len(symbols),
          "historical_price_coverage":coverage,"results":report,
          "limitations":["Exploratory survivor universe and historical prices from current vendor",
          "Historical original scores only from available August-October 2026 snapshots",
          "Censored trades cannot be treated as losses",
          "Returns exclude trading costs/slippage","Intraday simultaneous target and stop assumes stop first"],
          "entry_rule":"next session opening price; confirmation requires signal-day close > SMA10, daily close increasing, SMA10 >= SMA20",
          "stop":"7 percent hard stop; adverse gap filled at session open"}
 (OUT/"summary.json").write_text(json.dumps(summary,indent=2))
 print(json.dumps({"signals":len(obs),"episodes":len(nonoverlap),"results":report},indent=2),flush=True)
if __name__=="__main__":main()
