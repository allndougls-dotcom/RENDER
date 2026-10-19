"""Prioritize feasible historical valuations without pretending splits are verified."""
import argparse,csv,json
from datetime import date
from pathlib import Path

def report(sec_dir,price_dir,asof):
    target=date.fromisoformat(asof)
    rows=[]
    for f in sorted(Path(sec_dir).glob("*.json")):
        if f.name=="coverage.json":continue
        sec=json.loads(f.read_text());ticker=sec["ticker"]
        file=Path(price_dir)/(ticker+".csv")
        corporate=Path(price_dir)/(ticker+".splits.json")
        checks={"asof_price":False,"has_actions":corporate.exists(),
                "eps_reported":False,"shares_reported":False,"equity_reported":False,
                "action_history_covers_financial_periods":False}
        facts=[((sec.get("eps") or {}).get("annual") or {})]
        fields=sec.get("fields") or {}
        facts += [(fields.get("shares") or {}),(fields.get("equity") or {})]
        for key,item in zip(("eps_reported","shares_reported","equity_reported"),facts):
            checks[key]=bool(item.get("value") is not None and item.get("period_end")
                             and item.get("available_on") and item["available_on"]<=asof)
        if file.exists():
            with file.open(newline="") as fh:
                checks["asof_price"]=any(r.get("Date","")[:10]==asof for r in csv.DictReader(fh))
        split_count=None
        if corporate.exists():
            actions=json.loads(corporate.read_text())
            split_count=len(actions.get("events") or [])
            try:
                start=date.fromisoformat(actions["history_start"])
                end=date.fromisoformat(actions["history_end"])
                periods=[date.fromisoformat(x["period_end"]) for x in facts if x.get("period_end")]
                checks["action_history_covers_financial_periods"]=(
                    start<=min(periods+[target]) and end>=target)
            except (KeyError,ValueError,TypeError):pass
        rows.append({"ticker":ticker,"checks":checks,"yahoo_split_events":split_count,
                     "all_inputs_available":all(checks.values()),
                     "valuation_verified":False})
    return {"asof":asof,"companies":len(rows),
        "input_complete":sum(x["all_inputs_available"] for x in rows),
        "no_split_events_reported":sum(x["yahoo_split_events"]==0 for x in rows),
        "missing_by_component":{k:sum(not x["checks"][k] for x in rows)
                                for k in (rows[0]["checks"] if rows else [])},
        "rows":rows,
        "warning":"Input completeness is not independent split or share-basis verification. Never score candidates."}
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--asof",required=True)
    p.add_argument("--sec",default="results/quality_swing/sec_pit")
    p.add_argument("--prices",default="results/quality_swing/valuation_prices")
    p.add_argument("--out",default="results/quality_swing/valuation_candidate_coverage.json")
    a=p.parse_args()
    data=report(a.sec,a.prices,a.asof)
    path=Path(a.out);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in data.items() if k!="rows"},indent=2))
if __name__=="__main__":main()
