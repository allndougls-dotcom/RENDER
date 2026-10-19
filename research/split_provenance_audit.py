"""Audit split-action history coverage for historical valuation inputs.
Coverage is a diagnostic, never independent verification of Yahoo corporate actions.
"""
import argparse,csv,json
from datetime import date
from pathlib import Path

def audit(sec_dir,price_dir,asof):
    asof_date=date.fromisoformat(asof)
    rows=[]
    for file in sorted(Path(sec_dir).glob("*.json")):
        if file.name=="coverage.json":continue
        sec=json.loads(file.read_text());ticker=sec["ticker"]
        p=Path(price_dir)/(ticker+".splits.json")
        reason=None;events=0
        if not p.exists():reason="missing_split_events_file"
        else:
            raw=json.loads(p.read_text());start=raw.get("history_start");end=raw.get("history_end")
            if not start or not end:reason="missing_history_bounds"
            else:
                try:
                    lo=date.fromisoformat(start);hi=date.fromisoformat(end)
                    if not(lo<=asof_date<=hi):reason="asof_outside_action_history"
                    relevant=[]
                    eps=(sec.get("eps") or {}).get("annual") or {}
                    for name in ("shares","equity"):
                        obj=(sec.get("fields") or {}).get(name) or {}
                        if obj.get("period_end"):relevant.append(date.fromisoformat(obj["period_end"]))
                    if eps.get("period_end"):relevant.append(date.fromisoformat(eps["period_end"]))
                    if relevant and lo>min(relevant):reason="action_history_starts_after_filing_period"
                    parsed=[]
                    for x in raw.get("events",[]):
                        dt=date.fromisoformat(x["date"]);ratio=float(x["ratio"])
                        if ratio<=0 or ratio==1 or not lo<=dt<=hi:reason="invalid_action_event"
                        parsed.append(dt)
                    if len(parsed)!=len(set(parsed)):reason="duplicate_split_dates"
                    events=len(parsed)
                except (ValueError,TypeError,KeyError):reason="invalid_action_history"
            if reason is None:reason="needs_independent_corporate_action_confirmation"
        rows.append({"ticker":ticker,"events":events,"verified":False,"blocker":reason})
    blockers={}
    for r in rows:blockers[r["blocker"]]=blockers.get(r["blocker"],0)+1
    return {"asof":asof,"companies":len(rows),"independently_verified":0,
            "blockers":blockers,"rows":rows,
            "note":"Completeness checks are not split verification. No automatic upgrade to verified."}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--sec",default="results/quality_swing/sec_pit")
    ap.add_argument("--prices",default="results/quality_swing/valuation_prices")
    ap.add_argument("--asof",required=True)
    ap.add_argument("--out",default="results/quality_swing/split_provenance_audit.json")
    a=ap.parse_args()
    report=audit(a.sec,a.prices,a.asof)
    output=Path(a.out);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!="rows"},indent=2))
if __name__=="__main__":main()
