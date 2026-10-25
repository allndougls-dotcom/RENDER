"""Research-only audit of historical backtest candidates and point-in-time provenance.

Reports evidence, not blanket validity judgements. No live files are modified.
"""
from pathlib import Path
import ast,csv,json,re,subprocess
OUT=Path("results/sidi_growth/backtest_provenance")
KEYS={
 "point_in_time_dates":r"\b(filed|accepted|available_at|asof|point.in.time|publication_date)\b",
 "historical_financials":r"\b(companyfacts|sec_pit|fundamentals_pit|pit_ratios|historical_valuation)\b",
 "current_fundamental_risk":r"\b(yf\.Ticker|\.info\b|ratios-ttm|profile/|_fund_yf|_fund_fmp)\b",
 "technical_conditions":r"\b(rsi|macd|dd60|drawdown|volume_ratio|setup_hot|spy_return)\b",
 "score_usage":r"\b(fund_score|combined_score|tech_score|score_threshold|score_min)\b",
 "signal_selection":r"\b(full_setup|white_bird|shadow|signal|candidate)\b",
 "price_outcomes":r"\b(tp_pct|stop_pct|target|take_profit|win_rate|outcome)\b",
 "survivorship_risk":r"\b(get_sp500_tickers|current_constituents|sp500_tickers|wikipedia)\b",
}
def report(path):
 text=path.read_text(encoding="utf-8",errors="replace")
 lines=text.splitlines()
 evidence={}
 for tag,pattern in KEYS.items():
  hits=[{"line":i,"excerpt":line.strip()[:170]} for i,line in enumerate(lines,1) if re.search(pattern,line,re.I) and not line.strip().startswith("#")]
  evidence[tag]=hits[:8]
 # Score freshness cannot be guaranteed by string matching; source documents need review.
 risks=[]
 if evidence["current_fundamental_risk"]:risks.append("Potential present-day financial input or undocumented as-of source")
 if evidence["survivorship_risk"]:risks.append("Potential current-universe membership")
 if evidence["score_usage"] and not evidence["historical_financials"]:risks.append("Scoring present without explicit PIT fundamentals reference in this file")
 if evidence["price_outcomes"] and not evidence["point_in_time_dates"]:risks.append("Backtest outcomes with no explicit dated fundamental proof in this file")
 return {"path":path.as_posix(),"evidence":evidence,"flags_for_manual_review":risks,
         "classification":"REQUIRES_EVIDENCE_REVIEW"}
def main():
 OUT.mkdir(parents=True,exist_ok=True)
 candidate=[]
 for base in (Path("research"),Path("backtest"),Path("backtests"),Path("modules")):
  if not base.exists():continue
  for p in base.rglob("*.py"):
   if any(part in ("__pycache__",".venv") for part in p.parts):continue
   if base.name=="modules" and not any(k in p.name.lower() for k in ("backtest","shadow","score","fundament","tecnico")):continue
   candidate.append(p)
 results=[report(p) for p in sorted(set(candidate))]
 focus=[r for r in results if r["evidence"]["price_outcomes"] or
        (r["evidence"]["signal_selection"] and r["evidence"]["score_usage"])]
 with (OUT/"file_provenance.csv").open("w",newline="",encoding="utf-8") as f:
  w=csv.DictWriter(f,fieldnames=("path","flags","pit_evidence","historical_financial_evidence",
                                  "current_fundamental_risk","score_usage","price_outcomes"))
  w.writeheader()
  for r in focus:
   e=r["evidence"]
   w.writerow({"path":r["path"],"flags":" | ".join(r["flags_for_manual_review"]),
      "pit_evidence":len(e["point_in_time_dates"]),"historical_financial_evidence":len(e["historical_financials"]),
      "current_fundamental_risk":len(e["current_fundamental_risk"]),
      "score_usage":len(e["score_usage"]),"price_outcomes":len(e["price_outcomes"])})
 summary={"files_scanned":len(results),"research_candidates":len(focus),
  "review_required":sum(bool(r["flags_for_manual_review"]) for r in focus),
  "files":focus,
  "interpretation":"Static discovery only: regex evidence does not prove a backtest valid or invalid.",
  "validation_required":["Trace each candidate to actual dated inputs and artifacts",
    "Verify filings were available by signal date, not fiscal period end",
    "Verify score sector medians and constituents as-of",
    "Re-run baselines with PIT inputs and holdout before reporting WR"]}
 (OUT/"summary.json").write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding="utf-8")
 print(json.dumps({"files_scanned":len(results),"candidates":len(focus),
  "flagged_for_review":summary["review_required"],"output":str(OUT)},indent=2))
if __name__=="__main__":main()
