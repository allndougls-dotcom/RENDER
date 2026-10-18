"""Join SEC point-in-time metrics with same-date technical gates; audit readiness.

A diagnostic integration, NOT a historical combined score. Never backfill today's
scores, medians, or valuations. A missing component stays null.
"""
import argparse,csv,json
from pathlib import Path
from ttm_metrics import compute

def audit(in_dir,tech_dir,asof):
    companies=[];missing={}
    for path in sorted(Path(in_dir).glob("*.json")):
        if path.name=="coverage.json":continue
        record=json.loads(path.read_text(encoding="utf-8"))
        if record["asof"]!=asof:raise ValueError(f"Mismatched asof {path}")
        metrics=compute(record)
        ticker=record.get("ticker")
        technical=Path(tech_dir)/(ticker+".csv")
        tech=None
        if technical.exists():
            with technical.open(newline="",encoding="utf-8") as fh:
                prior=[row for row in csv.DictReader(fh) if row.get("Date","")[:10]<=asof]
            if prior:tech=prior[-1]
        checks={
          "ttm_revenue":metrics["ttm"]["revenue"] is not None,
          "ttm_net_income":metrics["ttm"]["net_income"] is not None,
          "fcf_ni_ratio":metrics["metrics"]["fcf_ni_ratio_ttm"] is not None,
          "current_ratio":metrics["metrics"]["current_ratio"] is not None,
          "historical_technical_gates":tech is not None,
          "historical_tech_score":False,
          "validated_eps_growth":False,
          "historical_debt_equity":False,
          "historical_pe_pb_market_cap":False,
          "historical_sector_medians":False,
          "historical_membership":False,
        }
        for k,v in checks.items():
            if not v:missing[k]=missing.get(k,0)+1
        companies.append({"ticker":ticker,"asof":asof,"checks":checks,
                          "technical_asof":tech.get("Date","")[:10] if tech else None,
                          "combined_score":None})
    return {"companies":len(companies),"missing":missing,
            "valid_combined_scores":0,"backtest_ready":False,"rows":companies,
            "warning":"SEC metrics + technical gates are diagnostic; technical gates are not SIDI tech_score."}
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--in-dir",default="results/quality_swing/sec_pit")
    p.add_argument("--technical",default="results/quality_swing/technical_pit")
    p.add_argument("--asof",required=True)
    p.add_argument("--out",default="results/quality_swing/integrated_readiness.json")
    args=p.parse_args()
    result=audit(args.in_dir,args.technical,args.asof)
    target=Path(args.out);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k!="rows"},indent=2))
if __name__=="__main__":main()
