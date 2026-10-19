"""Join SEC point-in-time metrics with same-date technical gates; audit readiness.

A diagnostic integration, NOT a historical combined score. Never backfill today's
scores, medians, or valuations. A missing component stays null.
"""
import argparse,csv,json
from datetime import date
from pathlib import Path
from ttm_metrics import compute

def audit(in_dir,tech_dir,asof,score_file=None,valuation_file=None):
    valuations={}
    if valuation_file and Path(valuation_file).exists():
        vp=json.loads(Path(valuation_file).read_text(encoding='utf-8'))
        if vp.get('asof')!=asof:raise ValueError('Valuation date mismatch')
        valuations={r['ticker']:r['valuation'] for r in vp.get('rows',[]) if r.get('asof')==asof}
    scores={}
    if score_file and Path(score_file).exists():
        payload=json.loads(Path(score_file).read_text(encoding="utf-8"))
        scores={r["ticker"]:r for r in payload.get("rows",[]) if r.get("asof")==asof}
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
            if prior:
                candidate=prior[-1]
                age=(date.fromisoformat(asof)-date.fromisoformat(candidate["Date"][:10])).days
                if 0<=age<=7:tech=candidate
        eps_growth=(record.get("eps") or {}).get("annual_growth") or {}
        eps_sources_valid=(
            eps_growth.get("growth") is not None
            and eps_growth.get("tag") == "EarningsPerShareDiluted"
            and bool(eps_growth.get("available_on"))
            and eps_growth["available_on"] <= asof
            and bool(eps_growth.get("new_period_end"))
            and eps_growth["new_period_end"] <= asof
            and bool(eps_growth.get("old_period_end"))
            and eps_growth["old_period_end"] < eps_growth["new_period_end"]
        )
        val=valuations.get(ticker,{})
        val_ready=(val.get("valid") is True and
                   all(val.get(k) is not None for k in ("pe","pb","market_cap")))
        checks={
          "ttm_revenue":metrics["ttm"]["revenue"] is not None,
          "ttm_net_income":metrics["ttm"]["net_income"] is not None,
          "fcf_ni_ratio":metrics["metrics"]["fcf_ni_ratio_ttm"] is not None,
          "current_ratio":metrics["metrics"]["current_ratio"] is not None,
          "historical_technical_gates":tech is not None,
          "historical_tech_score":ticker in scores,
          "validated_eps_growth":eps_sources_valid,
          "historical_debt_equity":False,
          "historical_pe_pb_market_cap":val_ready,
          "historical_sector_medians":False,
          "historical_membership":False,
        }
        for k,v in checks.items():
            if not v:missing[k]=missing.get(k,0)+1
        companies.append({"ticker":ticker,"asof":asof,"checks":checks,
                          "technical_asof":tech.get("Date","")[:10] if tech else None,
                          "tech_score":scores[ticker]["tech_score"] if ticker in scores else None,
                          "eps_growth_annual_pit":eps_growth["growth"] if eps_sources_valid else None,
                          "historical_valuation":{k:val.get(k) for k in ("pe","pb","market_cap")} if val_ready else None,
                          "combined_score":None})
    return {"companies":len(companies),"missing":missing,
            "valid_combined_scores":0,"backtest_ready":False,"rows":companies,
            "warning":"SEC metrics + technical gates are diagnostic; technical gates are not SIDI tech_score."}
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--in-dir",default="results/quality_swing/sec_pit")
    p.add_argument("--technical",default="results/quality_swing/technical_pit")
    p.add_argument("--asof",required=True)
    p.add_argument("--score-file",default="results/quality_swing/exact_technical_scores.json")
    p.add_argument("--valuation-file",default="results/quality_swing/historical_valuations.json")
    p.add_argument("--out",default="results/quality_swing/integrated_readiness.json")
    args=p.parse_args()
    result=audit(args.in_dir,args.technical,args.asof,args.score_file,args.valuation_file)
    target=Path(args.out);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k!="rows"},indent=2))
if __name__=="__main__":main()
