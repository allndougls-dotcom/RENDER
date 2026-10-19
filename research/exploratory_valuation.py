"""Exploratory valuation using Yahoo actions; NEVER certified or used for SIDI score.

This is a diagnostic sensitivity study. It treats Yahoo split events as a
working assumption, not independent verification. Exact filing date gates remain.
"""
import argparse,csv,json
from pathlib import Path
from valuation_pit import historical_valuation

def compute(sec_dir,price_dir,asof):
    rows=[]
    for file in sorted(Path(sec_dir).glob("*.json")):
        if file.name=="coverage.json":continue
        sec=json.loads(file.read_text());ticker=sec["ticker"]
        price_file=Path(price_dir)/(ticker+".csv")
        split_file=Path(price_dir)/(ticker+".splits.json")
        row={"ticker":ticker,"asof":asof,"status":"missing_price_or_actions",
             "exploratory":True,"certified":False,"pe":None,"pb":None,"market_cap":None}
        if price_file.exists() and split_file.exists():
            splits=json.loads(split_file.read_text())
            with price_file.open(newline="") as f:
                prices=[x for x in csv.DictReader(f) if x.get("Date","")[:10]==asof]
            if prices:
                fields=sec.get("fields") or {}
                eps=(sec.get("eps") or {}).get("annual") or {}
                shares=fields.get("shares") or {}
                equity=fields.get("equity") or {}
                entries=(eps,shares,equity)
                historical=all(v.get("available_on") and v["available_on"]<=asof
                               and v.get("period_end") and v["period_end"]<=asof
                               for v in entries)
                start=splits.get("history_start","9999")
                end=splits.get("history_end","0000")
                complete=(historical and start<=min(v["period_end"] for v in entries)
                          and end>=asof)
                if complete:
                    v=historical_valuation(
                        asof=asof,price=prices[-1]["Close"],price_date=asof,
                        price_basis="yahoo_close_split_adjusted_no_dividends",
                        eps=eps.get("value"),eps_period_end=eps["period_end"],
                        shares=shares.get("value"),shares_period_end=shares["period_end"],
                        equity=equity.get("value"),equity_period_end=equity["period_end"],
                        split_events=splits.get("events"),
                        split_history_verified=True)
                    row.update({k:v.get(k) for k in ("pe","pb","market_cap")})
                    row["status"]="exploratory_not_verified" if v.get("valid") else v.get("reason")
                    row["warnings"]=v.get("warnings",[])
                else:row["status"]="filing_or_split_history_incomplete"
            else:row["status"]="historical_close_missing"
        rows.append(row)
    return {"asof":asof,"rows":rows,"companies":len(rows),"exploratory_only":True,
            "pe_candidates":sum(x["pe"] is not None for x in rows),
            "pb_candidates":sum(x["pb"] is not None for x in rows),
            "full_candidates":sum(all(x[k] is not None for k in ("pe","pb","market_cap")) for x in rows),
            "warning":"ASSUMPTION ONLY: Yahoo actions not independently validated. Never feed into SIDI scores, signals, or win rates."}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--asof",required=True)
    p.add_argument("--sec",default="results/quality_swing/sec_pit")
    p.add_argument("--prices",default="results/quality_swing/valuation_prices")
    p.add_argument("--out",default="results/quality_swing/exploratory_valuations.json")
    a=p.parse_args()
    data=compute(a.sec,a.prices,a.asof)
    out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(data,indent=2))
    print(json.dumps({k:v for k,v in data.items() if k!="rows"},indent=2))
if __name__=="__main__":main()
