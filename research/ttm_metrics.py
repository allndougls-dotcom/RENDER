"""Audited PIT TTM fundamental metrics. These are NOT complete SIDI scores.
No use of contemporary fundamental data, no substitution of missing historical debt.
"""
import argparse,json
from pathlib import Path

def value(d,name):
    v=(d.get("ttm") or {}).get(name)
    return None if not v else v.get("value")
def safe_div(a,b):
    return a/b if a is not None and b not in (None,0) else None
def compute(d):
    ttm={k:value(d,k) for k in ("revenue","net_income","operating_cashflow","capex")}
    fields=d.get("fields",{})
    def f(k):
        x=fields.get(k)
        if not x:return None
        if x.get("available_on","9999")>d["asof"]:raise ValueError("Future SEC disclosure")
        return x["value"]
    equity=f("equity")
    capex=ttm["capex"]
    ocf=ttm["operating_cashflow"]
    same_end=(d.get("ttm",{}).get("operating_cashflow") or {}).get("period_end")== (d.get("ttm",{}).get("capex") or {}).get("period_end")
    fcf=ocf-capex if ocf is not None and capex is not None and same_end else None
    prior=f("equity_prior")
    debt_parts=[f(x) for x in ("long_term_debt_noncurrent","long_term_debt_current","short_term_borrowings")]
    debt_dates=[(fields.get(x) or {}).get("period_end") for x in ("long_term_debt_noncurrent","long_term_debt_current","short_term_borrowings") if (fields.get(x) or {}).get("value") is not None]
    # At least the long-term current and noncurrent components must be reported.\n    # Do not treat an absent debt component as 0 or silently undercount borrowing.\n    long_components_present=all(x is not None for x in debt_parts[:2])\n    debt_consistent=(long_components_present and len(set(debt_dates))==1 and
                     debt_dates[0]==(fields.get("equity") or {}).get("period_end"))
    debt=sum(x for x in debt_parts if x is not None) if debt_consistent else None
    avg_equity=(equity+prior)/2 if equity is not None and prior is not None and equity>0 and prior>0 else None
    from datetime import date
    net_end=(d.get("ttm",{}).get("net_income") or {}).get("period_end")
    equity_end=(fields.get("equity") or {}).get("period_end")
    prior_end=(fields.get("equity_prior") or {}).get("period_end")
    roe_period_compatible=(net_end is not None and equity_end is not None
        and prior_end is not None
        and abs((date.fromisoformat(net_end)-date.fromisoformat(equity_end)).days)<=110
        and 330<=(date.fromisoformat(equity_end)-date.fromisoformat(prior_end)).days<=400)
    ratios={
      "fcf_ttm":fcf,
      "roe_ttm_avg_equity_exploratory":safe_div(ttm["net_income"],avg_equity) if roe_period_compatible else None,
      "debt_equity_interest_bearing_exploratory_pct":100*safe_div(debt,equity) if debt is not None and equity not in (None,0) else None,
      "fcf_ni_ratio_ttm":safe_div(fcf,ttm["net_income"]) if (d.get("ttm",{}).get("net_income") or {}).get("period_end")== (d.get("ttm",{}).get("operating_cashflow") or {}).get("period_end") else None,
      "net_margin_ttm":safe_div(ttm["net_income"],ttm["revenue"]) if (d.get("ttm",{}).get("net_income") or {}).get("period_end")== (d.get("ttm",{}).get("revenue") or {}).get("period_end") else None,
      "roe_ttm_over_latest_equity_proxy":safe_div(ttm["net_income"],equity),
      "current_ratio":safe_div(f("current_assets"),f("current_liabilities")),
      "liabilities_to_equity_proxy":safe_div(f("liabilities"),equity),
    }
    return {"ticker":d.get("ticker"),"asof":d["asof"],"metrics":ratios,
            "ttm":ttm,"historical_sidi_score":None,
            "caveats":["ROE requires period-average equity", "Liabilities are not interest-bearing debt",
             "FCF and net income require matched TTM period ends",
             "Historical market cap and sector contemporaneous medians missing",
             "Borrowings sum may omit issuer-specific debt tags; zero must not be inferred from missing tags",
             "Average equity proxy requires prior balance sheet, but timing may not match TTM period"]}
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--in-dir",default="results/quality_swing/sec_pit")
    p.add_argument("--out",default="results/quality_swing/ttm_ratio_coverage.json")
    a=p.parse_args()
    out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True)
    rows=[]
    for path in sorted(Path(a.in_dir).glob("*.json")):
        if path.name=="coverage.json":continue
        rows.append(compute(json.loads(path.read_text())))
    keys=list(rows[0]["metrics"]) if rows else []
    report={"companies":len(rows),"coverage":{k:sum(r["metrics"][k] is not None for r in rows) for k in keys},
       "historical_scores":0,"warning":"These remain interim metrics, not FULL SIDI scores."}
    out.write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
if __name__=="__main__":main()
