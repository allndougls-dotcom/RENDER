"""Exploratory diagnostics: score bands, first-crossing versus follow-up entries,
and favorable/adverse excursions. Uses original Git CSV signals, no production edits.
"""
import csv,json,math
from collections import defaultdict
from pathlib import Path
import pandas as pd,yfinance as yf
from recover_original_ingesta_history import git,col
from original_ingesta_swing_outcomes import records,get_prices
OUT=Path("results/quality_swing/entry_diagnostics")
def main():
 OUT.mkdir(parents=True,exist_ok=True)
 signals=records()
 hist={}
 for t in sorted({s["ticker"] for s in signals}):
  hist[t]=get_prices(t,"2026-08-01","2026-10-22")
 episodes=[]
 for t in sorted(hist):
  rs=sorted([r for r in signals if r["ticker"]==t],key=lambda r:r["date"])
  dates=hist[t];last_idx=-999
  for r in rs:
   first=next((i for i,x in enumerate(dates) if x[0]>r["date"]),None)
   if first is None:continue
   # 20-session cooldown, more responsive than 40-session previous grouping.
   if first-last_idx<20:continue
   last_idx=first
   entry=dates[first][1];window=dates[first:first+20]
   if not window:continue
   best=max(q[2] for q in window)/entry-1
   worst_open=min(q[1] for q in window)/entry-1
   episodes.append({"ticker":t,"signal_date":r["date"],"score":float(r["combined_score"]),
      "entry_date":dates[first][0],"sessions_observed":len(window),
      "mature_20":len(window)==20,"max_high_pct":round(best*100,2),
      "worst_open_pct":round(worst_open*100,2),
      "hit_10":int(best>=.10),"hit_15":int(best>=.15),
      "score_band":"8.5+" if float(r["combined_score"])>=8.5 else ("8.25-8.5" if float(r["combined_score"])>=8.25 else "8.0-8.25")})
 fields=list(episodes[0]) if episodes else ["ticker","signal_date"]
 with (OUT/"episodes.csv").open("w",newline="") as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(episodes)
 bands={}
 for band in ("8.0-8.25","8.25-8.5","8.5+"):
  subset=[r for r in episodes if r["score_band"]==band]
  complete=[r for r in subset if r["mature_20"]]
  bands[band]={"episodes":len(subset),"20_session_complete":len(complete),
    "hit10_complete":sum(r["hit_10"] for r in complete),
    "hit15_complete":sum(r["hit_15"] for r in complete),
    "hit10_rate_mature":round(100*sum(r["hit_10"] for r in complete)/len(complete),1) if complete else None,
    "hit15_rate_mature":round(100*sum(r["hit_15"] for r in complete)/len(complete),1) if complete else None}
 result={"episodes":len(episodes),"bands":bands,
  "method":"Original signals; adjusted next-session open; 20-session nonoverlapping episodes; max high and minimum open. No stop, transaction costs, or survivorship correction.",
  "warning":"Very small sample. Score threshold crossings cannot be reconstructed for below-8 days from the >8 export alone."}
 (OUT/"summary.json").write_text(json.dumps(result,indent=2))
 print(json.dumps(result,indent=2))
if __name__=="__main__":main()
