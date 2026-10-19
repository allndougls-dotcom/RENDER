"""Exploratory SIDI-formula ranking; never a validated historical score.

Production scoring is reused, but incomplete metrics are left NaN (production
neutral defaults). Contemporary survivor universe/sector data and unverified
valuations make all results unsuitable for signal or WR claims.
"""
import argparse,csv,json,sys
from pathlib import Path
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"modules"/"ingesta"))
from scoring import _fund_score,_sector_medians
from ttm_metrics import compute

def rank(sec_dir,universe,valuations,technicals,asof):
    v=json.loads(Path(valuations).read_text())
    t=json.loads(Path(technicals).read_text())
    if v.get("asof")!=asof:raise ValueError("valuation date mismatch")
    sectors={r["ticker"]:r.get("sector","") for r in csv.DictReader(open(universe,encoding="utf-8-sig"))}
    prices={r["ticker"]:r for r in v.get("rows",[]) if r.get("asof")==asof and r.get("exploratory") and r.get("certified") is False}
    tech={r["ticker"]:r for r in t.get("rows",[]) if r.get("asof")==asof}
    records=[]
    for file in Path(sec_dir).glob("*.json"):
        if file.name=="coverage.json":continue
        d=json.loads(file.read_text());ticker=d.get("ticker")
        if d.get("asof")!=asof or ticker not in prices:continue
        m=compute(d);growth=(d.get("annual_growth") or {}).get("revenue_annual") or {}
        eps=(d.get("eps") or {}).get("annual_growth") or {}
        q=prices[ticker]
        values={"ticker":ticker,"sector":sectors.get(ticker),
          "revenue_growth":growth.get("growth") if growth.get("available_by","9999")<=asof else None,
          "eps_growth":eps.get("growth") if eps.get("available_on","9999")<=asof and eps.get("comparability")=="same_filing_diluted_eps" else None,
          "roe":m["metrics"]["roe_ttm_avg_equity_exploratory"],
          "debt_equity":m["metrics"]["debt_equity_interest_bearing_exploratory_pct"],
          "fcf_ni_ratio":m["metrics"]["fcf_ni_ratio_ttm"],
          "current_ratio":m["metrics"]["current_ratio"],
          "fcf":m["metrics"]["fcf_ttm"],
          "pe":q.get("pe"),"pb":q.get("pb"),"market_cap":q.get("market_cap")}
        records.append(values)
    if not records:return {"asof":asof,"rows":[],"ranked":0,"exploratory_gt_8_5":0,"validated_combined_scores":0}
    df=pd.DataFrame(records)
    medians=_sector_medians(df)
    rows=[]
    for _,row in df.iterrows():
        ticker=row["ticker"];technical=tech.get(ticker,{})
        ts=technical.get("tech_score")
        if ts is None:continue
        try:ts=float(ts)
        except (TypeError,ValueError):continue
        if not np.isfinite(ts) or not 0<=ts<=10:continue
        fund=float(_fund_score(row,medians)["fund_score"])
        combined=round(0.6*fund+0.4*ts,2)
        missing=[k for k in ("revenue_growth","eps_growth","roe","debt_equity","fcf_ni_ratio","current_ratio","fcf","pe","pb","market_cap") if pd.isna(row.get(k))]
        rows.append({"ticker":ticker,"asof":asof,"sector_current":row["sector"],
          "fund_score_exploratory":fund,"tech_score_historical":ts,
          "combined_exploratory":combined,"above_8_5_exploratory":combined>8.5,
          "neutral_imputation_fields":missing,"certified":False,
          "eligible_historical_signal":False})
    rows.sort(key=lambda x:x["combined_exploratory"],reverse=True)
    return {"asof":asof,"rows":rows,"ranked":len(rows),
      "exploratory_gt_8_5":sum(x["above_8_5_exploratory"] for x in rows),
      "validated_combined_scores":0,"backtest_ready":False,
      "warning":"Exploratory only: production formula with neutral NaN defaults, proxy/missing SEC metrics, current sector and survivor universe, unverified valuations. Do not trade or calculate WR."}

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--asof",required=True)
    p.add_argument("--sec",default="results/quality_swing/sec_pit")
    p.add_argument("--universe",default="results/quality_swing/sec_universe.csv")
    p.add_argument("--valuations",default="results/quality_swing/exploratory_valuations.json")
    p.add_argument("--technicals",default="results/quality_swing/exact_technical_scores.json")
    p.add_argument("--out",default="results/quality_swing/exploratory_ranking.json")
    a=p.parse_args()
    result=rank(a.sec,a.universe,a.valuations,a.technicals,a.asof)
    out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!="rows"},indent=2))
    print("TOP 10",json.dumps(result["rows"][:10],indent=2))
