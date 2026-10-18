"""Historical SEC fundamentals: conservative ratio reconstruction and eligibility report.
Never substitute today's metrics. No SIDI score is emitted without historical valuation,
sector peer medians and technical score.
"""
import argparse, csv, json
from pathlib import Path
from datetime import date

FIELDS=("revenue","net_income","assets","liabilities","equity","current_assets",
        "current_liabilities","operating_cashflow","capex","shares")
def compute(data):
    asof=date.fromisoformat(data["asof"])
    fields=data["fields"]
    def get(name):
        field=fields.get(name)
        if not field:return None
        filed=date.fromisoformat(field["available_on"])
        end=date.fromisoformat(field["period_end"])
        if filed>asof or end>asof:raise ValueError("Future disclosure")
        return float(field["value"])
    values={k:get(k) for k in FIELDS}
    def div(a,b):
        return a/b if a is not None and b is not None and b!=0 else None
    ratio={
      "roe_proxy":div(values["net_income"],values["equity"]),
      "current_ratio":div(values["current_assets"],values["current_liabilities"]),
      "debt_equity_proxy":div(values["liabilities"],values["equity"]),
      "fcf_proxy":(values["operating_cashflow"]-values["capex"])
         if values["operating_cashflow"] is not None and values["capex"] is not None else None,
    }
    # The input's flow periods are Q or annual, NOT necessarily trailing twelve months.
    # Debt/equity proxy uses total liabilities, not interest-bearing debt; ROE proxy
    # uses a single disclosed net-income duration, not validated annual earnings.
    return {"ticker":data.get("ticker"),"asof":data["asof"],"ratios":ratio,
            "inputs_present":{k:values[k] is not None for k in FIELDS},
            "comparable_sidi_score":False,
            "reason":"Require aligned TTM periods, true debt definition, historical market cap, PE/PB, sector medians and technical score."}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--in-dir",default="results/quality_swing/sec_pit")
    p.add_argument("--out",default="results/quality_swing/ratio_coverage.json")
    args=p.parse_args()
    reports=[]; failures=[]
    for path in sorted(Path(args.in_dir).glob("*.json")):
        if path.name=="coverage.json":continue
        try:reports.append(compute(json.loads(path.read_text(encoding="utf-8"))))
        except (ValueError,KeyError,TypeError) as exc:failures.append({"file":path.name,"error":str(exc)})
    keys=("roe_proxy","current_ratio","debt_equity_proxy","fcf_proxy")
    coverage={k:sum(x["ratios"][k] is not None for x in reports) for k in keys}
    result={"processed":len(reports),"ratio_coverage":coverage,"failed":failures,
            "point_in_time_combined_scores":0,"valid_backtest_signals":0,
            "notes":"Ratio proxies are diagnostic only; never equate to SIDI historical score."}
    out=Path(args.out);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps(result,indent=2))

if __name__=="__main__":main()
