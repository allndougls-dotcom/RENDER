"""Coverage gate for actual independently reconstructed exploratory rankings."""
import argparse,csv,json
from pathlib import Path

def inspect(directory):
    reports=[]
    for f in sorted(Path(directory).glob("*.json")):
        p=json.loads(f.read_text())
        if f.stem!=p.get("asof"):raise ValueError(f"Misnamed ranking: {f}")
        rows=p.get("rows",[])
        keys=[r.get("ticker") for r in rows]
        if not all(keys) or len(keys)!=len(set(keys)):raise ValueError("Duplicate/missing ticker")
        if any(r.get("eligible_historical_signal") is not False for r in rows):
            raise ValueError("Unexpected certified signals")
        reports.append({"asof":p["asof"],"ranked":len(rows),
          "gt_8_5":sum(r.get("combined_exploratory",0)>8.5 for r in rows),
          "between_6_5_8_5":sum(6.5<=r.get("combined_exploratory",0)<=8.5 for r in rows),
          "with_neutral_imputation":sum(bool(r.get("neutral_imputation_fields")) for r in rows),
          "roe_available":sum("roe" not in r.get("neutral_imputation_fields",[]) for r in rows),
          "debt_equity_available":sum("debt_equity" not in r.get("neutral_imputation_fields",[]) for r in rows),
          "both_available":sum(not ({"roe","debt_equity"} & set(r.get("neutral_imputation_fields",[]))) for r in rows)})
    if not reports:raise ValueError("No independently dated rankings")
    return {"dates":len(reports),"per_date":reports,
            "gt_8_5_total":sum(r["gt_8_5"] for r in reports),
            "between_6_5_8_5_total":sum(r["between_6_5_8_5"] for r in reports),
            "validated_win_rate":None,"validated_backtest":False,
            "warning":"Cohort counts, not independent trades; single ticker may recur and dates overlap."}
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--rankings",default="results/quality_swing/dated_rankings")
    p.add_argument("--out",default="results/quality_swing/multidate_coverage.json")
    a=p.parse_args();result=inspect(a.rankings)
    o=Path(a.out);o.parent.mkdir(parents=True,exist_ok=True)
    o.write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
if __name__=="__main__":main()
