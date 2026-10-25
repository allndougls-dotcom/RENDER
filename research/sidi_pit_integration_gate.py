"""Historical SIDI PIT input integration and strict readiness gate (research-only).

Consumes files emitted by sec_pit_batch.py and does not use current fundamentals.
Never emits a historical SIDI score from incomplete or proxy values.
"""
import argparse,csv,json,math
from collections import Counter
from datetime import date
from pathlib import Path
REQUIRED=("revenue_growth","eps_growth","roe","debt_equity","current_ratio",
          "fcf_ni_ratio","fcf","market_cap","pe","pb","tech_score")
def val(x):
    if isinstance(x,dict):x=x.get("value")
    try:
        f=float(x)
        return f if math.isfinite(f) else None
    except (ValueError,TypeError):return None
def safe_div(a,b):
    return a/b if a is not None and b is not None and b!=0 else None
def verify_dates(node,asof):
    if not isinstance(node,dict):return False
    try:
        cutoff=date.fromisoformat(asof)
        filed=node.get("available_on",node.get("filed"))
        end=node.get("period_end",node.get("end"))
        if not filed or not end:return False
        return date.fromisoformat(filed)<=cutoff and date.fromisoformat(end)<=cutoff
    except (ValueError,TypeError):return False
def summarize(data):
    asof=data.get("asof","")
    fields=data.get("fields") or {}
    ttm=data.get("ttm") or {}
    annual=data.get("annual_growth") or {}
    eps=data.get("eps") or {}
    result={"ticker":data.get("ticker"),"asof":asof,"source":data.get("source"),
            "inputs":{},"provenance":{},"issues":[]}
    values={}
    for metric in ("revenue","net_income","operating_cashflow","capex"):
        node=ttm.get(metric)
        if node and node.get("method") in ("ANNUAL","FOUR_QUARTERS") and node.get("sources"):
            verified=all(verify_dates(s,asof) for s in node["sources"])
            values[metric]=val(node) if verified else None
            result["provenance"][metric]={"verified":verified,"method":node.get("method"),"sources":node.get("sources")}
        else:values[metric]=None
    for metric in ("equity","equity_prior","current_assets","current_liabilities",
                   "long_term_debt_noncurrent","long_term_debt_current","short_term_borrowings","shares"):
        node=fields.get(metric)
        verified=verify_dates(node,asof)
        values[metric]=val(node) if verified else None
        result["provenance"][metric]={"verified":verified,"source":node}
    # Debt and ROE use corrected TTM inputs, not total liabilities or single-quarter NI.
    eq=values["equity"];old=values["equity_prior"]
    avg_eq=(eq+old)/2 if eq is not None and old is not None else None
    result["inputs"]["roe"]=safe_div(values["net_income"],avg_eq)
    result["inputs"]["current_ratio"]=safe_div(values["current_assets"],values["current_liabilities"])
    debt_parts=("long_term_debt_noncurrent","long_term_debt_current","short_term_borrowings")
    debt=sum(values[k] for k in debt_parts) if all(values[k] is not None for k in debt_parts) else None
    result["inputs"]["debt_equity"]=safe_div(debt,eq)
    fcf=(values["operating_cashflow"]-values["capex"]) if values["operating_cashflow"] is not None and values["capex"] is not None else None
    result["inputs"]["fcf"]=fcf
    result["inputs"]["fcf_ni_ratio"]=safe_div(fcf,values["net_income"])
    # SEC helper's period-reconstructed annual fields need their own date provenance.
    # Until verified, never mark revenue/eps growth ready.
    result["inputs"]["revenue_growth"]=None
    result["inputs"]["eps_growth"]=None
    for k in ("pe","pb","market_cap","tech_score"):result["inputs"][k]=None
    missing=[k for k in REQUIRED if result["inputs"].get(k) is None]
    result["missing"]=missing
    result["partial_pit_ratio_ready"]=sum(result["inputs"][k] is not None for k in ("roe","debt_equity","current_ratio","fcf","fcf_ni_ratio"))
    result["full_score_ready"]=False
    result["score"]=None
    result["issues"].append("Independent SEC historical growth, split-adjusted asof valuations, production technical score, sector medians and historical constituent membership required.")
    return result
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--in-dir",default="results/quality_swing/sec_pit")
    p.add_argument("--out",default="results/sidi_growth/pit_integration_v1")
    a=p.parse_args()
    out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    rows=[];failures=[]
    for path in sorted(Path(a.in_dir).glob("*.json")):
        if path.name=="coverage.json":continue
        try:
            data=json.loads(path.read_text(encoding="utf-8"))
            if not data.get("asof"):raise ValueError("missing asof")
            rows.append(summarize(data))
        except (OSError,ValueError,TypeError) as exc:failures.append({"file":str(path),"reason":str(exc)})
    missing=Counter(m for r in rows for m in r["missing"])
    result={"input_directory":a.in_dir,"companies":len(rows),"complete_score_ready":0,
      "missing_by_input":dict(missing),"raw_ratio_component_counts":{
       k:sum(r["inputs"].get(k) is not None for r in rows) for k in REQUIRED},
      "failures":failures,"rows":rows,
      "caution":"No full combined score is emitted. Missing original vendor definitions, dated market valuation/technical/sector membership. Some ratio calculations may not match FMP definitions."}
    (out/"coverage.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    with (out/"input_readiness.csv").open("w",newline="",encoding="utf-8") as f:
        cols=["ticker","asof","partial_pit_ratio_ready","full_score_ready",*REQUIRED]
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader()
        for r in rows:w.writerow({**{k:r.get(k) for k in cols},**r["inputs"]})
    print(json.dumps({k:v for k,v in result.items() if k!="rows"},indent=2))
if __name__=="__main__":main()
