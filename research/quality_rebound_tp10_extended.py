"""QUALITY REBOUND extended TP10 exploratory backtest.

Original SIDI scores are only available since August 2026. This experiment
therefore reports two cohorts SEPARATELY:
* PIT original score signals (no hindsight score recomputation).
* Historical price-only candidate opportunities (no historical SIDI scores);
  NEVER interpret that cohort as a validated SIDI strategy backtest.
"""
import csv,json,math
from datetime import date,timedelta
from collections import defaultdict
from pathlib import Path
import pandas as pd,yfinance as yf
from quality_75_correction_swing import snapshots,prices
OUT=Path("results/quality_swing/quality_rebound_tp10_extended")
STOPS=(5,7,10)
HORIZONS=(40,60,90)
def outcome(bars,start,stop,horizon):
 entry=bars[start]["open"]; tp=entry*1.10; sl=entry*(1-stop/100)
 w=bars[start:start+horizon]
 for i,b in enumerate(w):
  if b["open"]<=sl:return "SL",100*(b["open"]/entry-1),i+1
  if b["open"]>=tp:return "TP",100*(b["open"]/entry-1),i+1
  if b["low"]<=sl:return "SL",-stop,i+1
  if b["high"]>=tp:return "TP",10.0,i+1
 if len(w)<horizon:return "CENSORED",None,len(w)
 return "TIME",100*(w[-1]["close"]/entry-1),len(w)
def annual_candidates(bars):
 # Control without historical SIDI scores, not an SIDI model.
 # Scan each bar, then enforce minimum 40 trading sessions between episodes.
 result=[];last=-100
 for i in range(60,len(bars)-1):
  if i-last<40:continue
  look=bars[i-59:i+1]
  correction=100*(bars[i]["close"]/max(x["high"] for x in look)-1)
  if correction<=-15:
   result.append((i,round(correction,2)));last=i
 return result
def main():
 OUT.mkdir(parents=True,exist_ok=True)
 original=snapshots()
 by_symbol=defaultdict(list)
 for s in original:
  if 7<=s["score"]<8:by_symbol[s["ticker"]].append(s)
 symbols=sorted(by_symbol)
 cohort_rows=[];coverage=[]
 end=(date.today()+timedelta(days=1)).isoformat()
 for ix,symbol in enumerate(symbols,1):
  bars=prices(symbol,"2022-01-01",end)
  coverage.append({"ticker":symbol,"bars":len(bars)})
  if len(bars)<100:continue
  index={b["date"]:j for j,b in enumerate(bars)}
  last_original=-100
  for s in sorted(by_symbol[symbol],key=lambda x:x["date"]):
   i=index.get(s["date"])
   if i is None or i<60 or i+1>=len(bars) or i-last_original<40:continue
   correction=100*(bars[i]["close"]/max(b["high"] for b in bars[i-59:i+1])-1)
   if correction>-15:continue
   last_original=i
   for stop in STOPS:
    for horizon in HORIZONS:
     status,pct,days=outcome(bars,i+1,stop,horizon)
     cohort_rows.append({"cohort":"original_sidi_pit","ticker":symbol,"signal_date":s["date"],
      "score":s["score"],"correction_pct":round(correction,2),"entry_date":bars[i+1]["date"],
      "stop_pct":stop,"tp_pct":10,"horizon":horizon,"outcome":status,
      "gross_return_pct":round(pct,3) if pct is not None else "","sessions":days})
  for i,correction in annual_candidates(bars):
   if bars[i]["date"]>="2026-08-01":continue
   for stop in STOPS:
    for horizon in HORIZONS:
     status,pct,days=outcome(bars,i+1,stop,horizon)
     cohort_rows.append({"cohort":"price_only_control_NO_SIDI_SCORE","ticker":symbol,
      "signal_date":bars[i]["date"],"score":"","correction_pct":correction,
      "entry_date":bars[i+1]["date"],"stop_pct":stop,"tp_pct":10,"horizon":horizon,
      "outcome":status,"gross_return_pct":round(pct,3) if pct is not None else "","sessions":days})
  if ix%50==0:print("Processed",ix,"of",len(symbols),flush=True)
 fields=("cohort","ticker","signal_date","score","correction_pct","entry_date",
         "stop_pct","tp_pct","horizon","outcome","gross_return_pct","sessions")
 with (OUT/"episodes.csv").open("w",newline="",encoding="utf-8") as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(cohort_rows)
 matrix=[]
 for cohort in ("original_sidi_pit","price_only_control_NO_SIDI_SCORE"):
  for stop in STOPS:
   for h in HORIZONS:
    r=[x for x in cohort_rows if x["cohort"]==cohort and x["stop_pct"]==stop and x["horizon"]==h]
    resolved=[x for x in r if x["outcome"]!="CENSORED"]
    matrix.append({"cohort":cohort,"stop_pct":stop,"sessions":h,
      "episodes":len(r),"resolved":len(resolved),"censored":len(r)-len(resolved),
      "target_hits":sum(x["outcome"]=="TP" for x in resolved),
      "stops":sum(x["outcome"]=="SL" for x in resolved),
      "time_exits":sum(x["outcome"]=="TIME" for x in resolved),
      "resolved_tp_rate_pct":round(100*sum(x["outcome"]=="TP" for x in resolved)/len(resolved),2) if resolved else None,
      "mean_resolved_gross_pct":round(sum(float(x["gross_return_pct"]) for x in resolved)/len(resolved),2) if resolved else None})
 summary={"matrix":matrix,"covered_tickers":len(symbols),"price_coverage":coverage,
  "non_equivalence":"Historical price-only cohort HAS NO SIDI SCORES and uses today's surviving ticker list: it is not a historical SIDI backtest.",
  "method":"40-session cooldown within each ticker/cohort; TP +10%; gap-aware SL; conservative SL precedence for same-day TP/SL; gross returns only; no portfolio execution.",
  "limitations":["Current Yahoo adjusted price histories","Historical membership and corporate action bias",
     "Score-based signals exist only in archives since August 2026","No transaction fees or portfolio constraints",
     "Incomplete time horizons censored and excluded from resolved rates"]}
 (OUT/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
 print(json.dumps({"signals_total":len(cohort_rows),"matrix":matrix},indent=2),flush=True)
if __name__=="__main__":main()
