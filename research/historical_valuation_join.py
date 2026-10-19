"""Audit point-in-time SEC and valuation-price joins without inventing ratios."""
import argparse,csv,json
from pathlib import Path
from valuation_pit import historical_valuation

def build(sec_dir,price_dir,asof):
    rows=[]
    for file in sorted(Path(sec_dir).glob("*.json")):
        if file.name=="coverage.json":continue
        sec=json.loads(file.read_text())
        ticker=sec["ticker"]
        prices=Path(price_dir)/(ticker+".csv")
        actions=Path(price_dir)/(ticker+".splits.json")
        valuation={"valid":False,"reason":"price_or_actions_missing"}
        if prices.exists() and actions.exists():
            splits=json.loads(actions.read_text())
            with prices.open(newline="") as fh:
                matching=[r for r in csv.DictReader(fh) if r["Date"][:10]==asof]
            if not matching:
                valuation={"valid":False,"reason":"asof_price_missing"}
            else:
                fields=sec.get("fields") or {}
                eps=(sec.get("eps") or {}).get("annual") or {}
                shares=fields.get("shares") or {}
                equity=fields.get("equity") or {}
                valuation=historical_valuation(
                    asof=asof,price=matching[-1]["Close"],price_date=asof,
                    price_basis="yahoo_close_split_adjusted_no_dividends",
                    eps=eps.get("value"),eps_period_end=eps.get("period_end"),
                    shares=shares.get("value"),shares_period_end=shares.get("period_end"),
                    equity=equity.get("value"),equity_period_end=equity.get("period_end"),
                    split_events=splits.get("events"),
                    split_history_verified=splits.get("split_history_verified",False))
        rows.append({"ticker":ticker,"asof":asof,"valuation":valuation})
    blocked={}
    for row in rows:
        reason=row["valuation"].get("reason")
        if reason:blocked[reason]=blocked.get(reason,0)+1
    return {"companies":len(rows),"asof":asof,
            "pe_coverage":sum(r["valuation"].get("pe") is not None for r in rows),
            "pb_coverage":sum(r["valuation"].get("pb") is not None for r in rows),
            "market_cap_coverage":sum(r["valuation"].get("market_cap") is not None for r in rows),
            "blocked":blocked,"rows":rows}
if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--asof",required=True)
    p.add_argument("--sec",default="results/quality_swing/sec_pit")
    p.add_argument("--prices",default="results/quality_swing/valuation_prices")
    p.add_argument("--out",default="results/quality_swing/historical_valuations.json")
    a=p.parse_args()
    result=build(a.sec,a.prices,a.asof)
    out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!="rows"},indent=2))
