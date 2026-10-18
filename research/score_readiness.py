"""Audit availability of the exact SIDI combined score components at a historical date.

This is an eligibility audit, NOT a score estimator. No value is filled from present.
SIDI combined=0.6*fund_score+0.4*tech_score, with fundamentals normalized to
cross-sectional historical sector medians. ROE/FCF proxies must not pass.
"""
import argparse, csv, json
from pathlib import Path

REQUIRED_FUND=("revenue_growth","eps_growth","roe","debt_equity","pe","pb",
               "current_ratio","fcf_ni_ratio","fcf","market_cap")
REQUIRED_TECH=("tech_score",)
def audit(records):
    result={"companies":len(records),"ready":0,"not_ready":0,"missing":{}, "not_comparable":[]}
    for d in records:
        fields=d.get("fields",{})
        ticker=d.get("ticker","unknown")
        missing=list(REQUIRED_FUND)+list(REQUIRED_TECH)+["historical_sector_medians","historical_index_membership"]
        # SEC PIT extraction currently contains only basic financial statement tags,
        # not validated TTM, exact debt, price-derived valuation or technical score.
        result["not_ready"]+=1
        result["not_comparable"].append({"ticker":ticker,"missing":missing})
        for field in missing: result["missing"][field]=result["missing"].get(field,0)+1
    result["quality_gt_8_5_count"]=None
    result["valid_backtest_signals"]=0
    result["warning"]="No reconstruction of exact scores. SEC PIT raw statements are not sufficient for a valid historical SIDI combined score."
    return result
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--in-dir",default="results/quality_swing/sec_pit")
    p.add_argument("--out",default="results/quality_swing/score_readiness.json")
    a=p.parse_args()
    records=[]
    for file in sorted(Path(a.in_dir).glob("*.json")):
        if file.name=="coverage.json":continue
        records.append(json.loads(file.read_text(encoding="utf-8")))
    report=audit(records)
    out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="not_comparable"},indent=2))
if __name__=="__main__":main()
