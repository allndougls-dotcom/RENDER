"""Descarga series OHLCV ajustadas por splits/dividendos para QUALITY SWING.
Usa exclusivamente tickers presentes en snapshots fechados; no toca producción.
"""
import argparse
import csv
import time
from pathlib import Path
import yfinance as yf

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--snapshots",default="data/master")
    ap.add_argument("--prices",default="data/backtest_prices")
    ap.add_argument("--years",type=int,default=10)
    ap.add_argument("--universe-csv",default=None)
    ap.add_argument("--limit",type=int,default=None)
    args=ap.parse_args()
    names=set()
    source_files=[Path(args.universe_csv)] if args.universe_csv else list(Path(args.snapshots).glob("sp500_full_export_*.csv"))
    for snapshot in source_files:
        with snapshot.open(encoding="utf-8-sig",newline="") as f:
            for row in csv.DictReader(f):
                tick=(row.get("ticker") or "").strip().upper()
                if tick:names.add(tick)
    target=Path(args.prices);target.mkdir(parents=True,exist_ok=True)
    successes=0;fails=[]
    selected=sorted(names)[:args.limit] if args.limit else sorted(names)
    for index,ticker in enumerate(selected,1):
        output=target/f"{ticker}.csv"
        if output.is_file():successes+=1;continue
        try:
            data=yf.download(ticker,period=f"{args.years}y",interval="1d",auto_adjust=True,
                             progress=False,threads=False)
            if data.empty or len(data)<30:raise ValueError("insufficient bars")
            if isinstance(data.columns,__import__("pandas").MultiIndex):
                data.columns=data.columns.get_level_values(0)
            data=data.reset_index()
            if "Date" not in data.columns:raise ValueError("missing Date column")
            data[["Date","Open","High","Low","Close","Volume"]].to_csv(output,index=False)
            successes+=1
        except Exception as exc:
            fails.append((ticker,str(exc)[:100]))
        if index%20==0:print(f"{index}/{len(selected)} downloaded or cached; failed {len(fails)}",flush=True)
        time.sleep(.3)
    print(f"PRICE COVERAGE: {successes}/{len(names)}; failed {len(fails)}")
    if fails:
        (target/"_failures.txt").write_text("\n".join(f"{t}: {reason}" for t,reason in fails),encoding="utf-8")

if __name__=="__main__":main()
