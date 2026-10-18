"""Reconstruction of SIDI historical technical gates from OHLCV, point-in-time.

No historical FUND scores are inferred here. Output explicitly marks score unavailable.
Standalone exploratory diagnostics, NOT a valid QUALITY SWING historical backtest.
"""
import argparse
import csv
import json
from pathlib import Path
import numpy as np
import pandas as pd

def reconstruct(path):
    d=pd.read_csv(path)
    for col in ("Date","Open","High","Low","Close","Volume"):
        if col not in d: raise ValueError(f"{path}: missing {col}")
    d["Date"]=pd.to_datetime(d["Date"],errors="coerce")
    for col in ("Open","High","Low","Close","Volume"):
        d[col]=pd.to_numeric(d[col],errors="coerce")
    d=d.dropna(subset=["Date","Close","High","Low","Volume"]).sort_values("Date")
    d=d.drop_duplicates("Date",keep="last").reset_index(drop=True)
    close=d["Close"]
    change=close.diff()
    gains=change.clip(lower=0).rolling(14,min_periods=14).mean()
    losses=(-change.clip(upper=0)).rolling(14,min_periods=14).mean()
    rsi=100-100/(1+gains/losses.replace(0,np.nan))
    macd=(close.ewm(span=12,adjust=False).mean()-close.ewm(span=26,adjust=False).mean())
    hist=macd-macd.ewm(span=9,adjust=False).mean()
    high60=close.rolling(60,min_periods=60).max()
    dd=100*(high60-close)/high60
    vol5=d["Volume"].rolling(5,min_periods=5).mean()
    vol60=d["Volume"].rolling(60,min_periods=60).mean()
    # As in production: individual gates, no forward-looking pivots.
    d["rsi_14"]=rsi
    d["drawdown_60d"]=dd
    d["macd_hist"]=hist
    d["sidi_pass_dd12"]=dd>=12
    d["sidi_pass_rsi40"]=rsi<40
    d["sidi_pass_macd"]=hist>hist.shift(1)
    d["sidi_pass_volume"]=vol5<vol60
    d["setup_hot"]=(d["sidi_pass_dd12"]&d["sidi_pass_rsi40"]&
                    d["sidi_pass_macd"]&d["sidi_pass_volume"])
    # No fake quality scores: fundamentals + historical sector statistics required.
    return d.loc[d["Date"].notna(),["Date","Open","High","Low","Close",
                 "rsi_14","drawdown_60d","sidi_pass_dd12","sidi_pass_rsi40",
                 "sidi_pass_macd","sidi_pass_volume","setup_hot"]]

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--prices",default="data/backtest_prices")
    p.add_argument("--out",default="results/quality_swing/technical_pit")
    args=p.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    files=list(Path(args.prices).glob("*.csv"))
    counts={"price_files":len(files),"processed":0,"bars":0,"technical_hot_bars":0,"failures":[]}
    for path in files:
        try:
            frame=reconstruct(path)
            frame.to_csv(out/(path.stem+".csv"),index=False)
            counts["processed"]+=1;counts["bars"]+=len(frame)
            counts["technical_hot_bars"]+=int(frame["setup_hot"].sum())
        except Exception as exc:
            counts["failures"].append({"ticker":path.stem,"reason":str(exc)[:150]})
    counts["combined_score_reconstructed"]=False
    counts["warning"]="Only technical gates. No historical fundamental score, so no WR is valid."
    (out.parent/"technical_coverage.json").write_text(json.dumps(counts,indent=2),encoding="utf-8")
    print(json.dumps(counts,indent=2))
if __name__=="__main__":main()
