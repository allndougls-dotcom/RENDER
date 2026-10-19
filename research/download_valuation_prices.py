"""Research-only historical split-only price feed (not the trading OHLCV)."""
import argparse, csv, json
from pathlib import Path
import yfinance as yf

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--universe-csv",default="results/quality_swing/sec_universe.csv")
    p.add_argument("--out",default="results/quality_swing/valuation_prices")
    p.add_argument("--limit",type=int,default=50)
    a=p.parse_args()
    out=Path(a.out);out.mkdir(parents=True,exist_ok=True)
    tickers=[r["ticker"].strip().upper() for r in csv.DictReader(open(a.universe_csv,encoding="utf-8-sig"))][:a.limit]
    report={"downloaded":0,"failed":[],"basis":"yahoo_close_split_adjusted_no_dividends"}
    for ticker in tickers:
        try:
            bars=yf.Ticker(ticker).history(period="max",auto_adjust=False,actions=True)
            if bars.empty or "Stock Splits" not in bars.columns:raise ValueError("missing bars or splits column")
            bars.index=bars.index.tz_localize(None) if bars.index.tz is not None else bars.index
            bars=bars.reset_index()
            if "Date" not in bars.columns:raise ValueError("missing Date")
            bars[["Date","Close"]].to_csv(out/(ticker+".csv"),index=False)
            events=[{"date":str(row["Date"].date()),"ratio":float(row["Stock Splits"])}
                    for _,row in bars.iterrows() if float(row["Stock Splits"])!=0]
            (out/(ticker+".splits.json")).write_text(json.dumps({
              "events":events,"source":"Yahoo corporate actions",
              "history_start":str(bars["Date"].min().date()),
              "history_end":str(bars["Date"].max().date()),
              "split_history_verified":False},indent=2))
            report["downloaded"]+=1
        except Exception as exc:report["failed"].append({"ticker":ticker,"reason":str(exc)[:120]})
    (out/"coverage.json").write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
if __name__=="__main__":main()
