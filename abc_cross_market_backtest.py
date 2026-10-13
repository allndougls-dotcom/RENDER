#!/usr/bin/env python3
"""
SIDI ABC cross-market validation.

Markets:
- FTSE 100 (UK)
- Nikkei 225 (Japan)
- S&P/TSX 60 (Canada)
- ASX 200 (Australia)

Frozen SIDI logic:
RSI<40, DD60<=-12%, MACD hist improving, vol5<vol60,
market20<=+1%, abnormal20<=-10%, fund_score>=6.5,
plus ABC:
A range_pos60>=0.088
B gap_pct>=-0.17
C 0.848<=vol5/vol60<1.0

Cross-market adaptation:
- market factor = local benchmark index
- sector factor = equal-weight return index built from local constituents in same sector
"""
from __future__ import annotations

import io, json, time
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from backtest import build_indicators
from modules.ingesta.fundamental import _fund_yf
from modules.ingesta.scoring import _fund_score, _sector_medians
from modules.ingesta.sidi_context import _abnormal20, _close_series, _spy20
from pattern_backtest import build_episode_dataset, enrich_signals, metrics as episode_metrics
from risk_matrix_backtest import make_price_maps, simulate_configuration
from stabilization_gate_backtest import (
    A_RANGE_POS60_MIN, B_GAP_PCT_MIN, C_VOL_RATIO_MIN, C_VOL_RATIO_MAX,
    to_entries_by_date,
)

MODEL_VERSION="SIDI_ABC_CROSS_MARKET_V1"
STRATEGY_VERSION="SIDI_INTRADAY_V2"
YEARS=8
OUT=Path("data/abc_cross_market")

MARKETS={
    "FTSE100":{
        "url":"https://en.wikipedia.org/wiki/FTSE_100_Index",
        "benchmark":"^FTSE","suffix":".L","min_rows":80,
    },
    "NIKKEI225":{
        "url":"https://en.wikipedia.org/wiki/Nikkei_225",
        "benchmark":"^N225","suffix":".T","min_rows":150,
    },
    "TSX60":{
        "url":"https://en.wikipedia.org/wiki/S%26P/TSX_60",
        "benchmark":"^GSPTSE","suffix":".TO","min_rows":40,
    },
    "ASX200":{
        "url":"https://en.wikipedia.org/wiki/S%26P/ASX_200",
        "benchmark":"^AXJO","suffix":".AX","min_rows":150,
    },
}

def fetch_table(market,cfg):
    r=requests.get(cfg["url"],headers={"User-Agent":"Mozilla/5.0 SIDI"},timeout=45)
    r.raise_for_status()
    tables=pd.read_html(io.StringIO(r.text))
    best=None
    for df in tables:
        cols=[str(c).strip() for c in df.columns]
        low=[c.lower() for c in cols]
        has_code=any(any(k in c for k in ["ticker","symbol","code","epic"]) for c in low)
        has_name=any(any(k in c for k in ["company","constituent","security"]) for c in low)
        if has_code and has_name and len(df)>=cfg["min_rows"]:
            best=df.copy(); best.columns=cols; break
    if best is None:
        raise RuntimeError(f"No constituent table for {market}")
    cmap={}
    for c in best.columns:
        cl=c.lower()
        if any(k in cl for k in ["ticker","symbol","code","epic"]): cmap[c]="raw_ticker"
        elif any(k in cl for k in ["company","constituent","security"]): cmap[c]="name"
        elif "sector" in cl or "industry" in cl: cmap[c]="sector"
    best=best.rename(columns=cmap)
    if "raw_ticker" not in best: raise RuntimeError(f"No ticker col {market}")
    if "sector" not in best: best["sector"]="Unknown"
    best=best[["raw_ticker","name","sector"]].copy()
    best["raw_ticker"]=best["raw_ticker"].astype(str).str.strip()
    if market=="NIKKEI225":
        best["ticker"]=best["raw_ticker"].str.extract(r"(\d{4})",expand=False)+cfg["suffix"]
    else:
        best["ticker"]=best["raw_ticker"].str.replace(".","-",regex=False)+cfg["suffix"]
    best=best.dropna(subset=["ticker"]).drop_duplicates("ticker")
    best["market"]=market
    return best

def download_prices(tickers,years=YEARS):
    end=datetime.today().strftime("%Y-%m-%d")
    start=(datetime.today()-timedelta(days=365*years+30)).strftime("%Y-%m-%d")
    out={}
    for i in range(0,len(tickers),40):
        chunk=tickers[i:i+40]
        try:
            raw=yf.download(chunk,start=start,end=end,auto_adjust=True,progress=False,group_by="ticker",threads=True)
        except Exception:
            continue
        for t in chunk:
            try:
                d=raw[t].copy() if isinstance(raw.columns,pd.MultiIndex) else raw.copy()
                d=d.reset_index().dropna(subset=["Close"])
                if len(d)>170: out[t]=d.reset_index(drop=True)
            except Exception: pass
        time.sleep(.4)
    return out

def download_benchmark(ticker,years=YEARS):
    p=download_prices([ticker],years)
    if ticker not in p: raise RuntimeError(f"Benchmark failed {ticker}")
    return _close_series(p[ticker])

def sector_indices(prices,meta):
    rets=[]
    for t,df in prices.items():
        sec=meta.get(t,{}).get("sector","Unknown")
        s=_close_series(df).pct_change().rename(t)
        tmp=pd.DataFrame({"ret":s,"sector":sec})
        tmp["date"]=tmp.index
        rets.append(tmp.reset_index(drop=True))
    allr=pd.concat(rets,ignore_index=True)
    out={}
    for sec,g in allr.groupby("sector"):
        s=g.groupby("date")["ret"].mean().dropna()
        idx=(1+s).cumprod()*100.0
        out[str(sec)]=idx
    return out

def build_prefund(prices,indicators,meta,benchmark,sector_idx):
    entries={}
    counts={"technical":0,"context":0,"prefund":0}
    for t,ind in indicators.items():
        if t not in meta: continue
        sec=meta[t]["sector"]
        sec_close=sector_idx.get(sec,pd.Series(dtype=float))
        stock=_close_series(prices[t])
        frame=prices[t].copy().reset_index(drop=True)
        date_to_idx={str(r["Date"])[:10]:i for i,r in frame.iterrows()}
        dates=ind["dates"]
        for i in range(62,len(dates)-1):
            rsi,dd,hp,hc,vd,atr=ind["rsi"][i],ind["dd60"][i],ind["hist"][i-1],ind["hist"][i],ind["vdec"][i],ind["atr"][i]
            if not (pd.notna(rsi) and rsi<40 and pd.notna(dd) and dd<=-12 and pd.notna(hp) and pd.notna(hc) and hc>hp and bool(vd) and pd.notna(atr) and atr>0): continue
            counts["technical"]+=1
            sd=dates[i]; cutoff=pd.Timestamp(sd).tz_localize(None).normalize()
            b=benchmark.loc[:cutoff]; s=sec_close.loc[:cutoff]; st=stock.loc[:cutoff]
            m20=_spy20(b)
            ab,bs,bc,_=_abnormal20(st,b,s) if len(s) else (np.nan,np.nan,np.nan,0)
            if not (np.isfinite(m20) and np.isfinite(ab)): continue
            counts["context"]+=1
            if m20>1.0 or ab>-10.0: continue
            ri=date_to_idx.get(sd)
            if ri is None or ri+1>=len(frame): continue
            nxt=frame.iloc[ri+1]; ep=float(nxt["Open"])
            if not np.isfinite(ep) or ep<=0: continue
            ed=str(nxt["Date"])[:10]
            entries.setdefault(ed,[]).append({
                "ticker":t,"signal_date":sd,"entry_date":ed,"entry_price":ep,"atr":float(atr),
                "fund_score":0.0,"dd60":float(dd),"rsi":float(rsi),
                "spy20":float(m20),"abnormal20":float(ab),"sector":sec,
                "market":meta[t]["market"],"beta_spy":float(bs),"beta_sector":float(bc),
            })
            counts["prefund"]+=1
    return entries,counts

def passes_abc(s):
    a=float(s.get("range_pos60",np.nan)); b=float(s.get("gap_pct",np.nan)); c=float(s.get("volume_ratio_5_60",np.nan))
    return np.isfinite(a) and np.isfinite(b) and np.isfinite(c) and a>=A_RANGE_POS60_MIN and b>=B_GAP_PCT_MIN and C_VOL_RATIO_MIN<=c<C_VOL_RATIO_MAX

def score_candidates(tickers,meta,medians):
    rows=[]
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs={ex.submit(_fund_yf,t):t for t in sorted(tickers)}
        for fut in as_completed(futs):
            t=futs[fut]
            try:d=fut.result()
            except Exception:d={}
            d["ticker"]=t; d["sector"]=meta[t]["sector"]; rows.append(d)
    scored=[]
    for d in rows:
        row=pd.Series(d); sc=_fund_score(row,medians)
        z=dict(d); z.update(sc.to_dict()); scored.append(z)
    return pd.DataFrame(scored)

def run_market(market,cfg,sp500_medians):
    uni=fetch_table(market,cfg)
    meta=uni.set_index("ticker").to_dict("index")
    prices=download_prices(uni["ticker"].tolist())
    benchmark=download_benchmark(cfg["benchmark"])
    inds=build_indicators(prices)
    sectors=sector_indices(prices,meta)
    prefund,counts=build_prefund(prices,inds,meta,benchmark,sectors)

    pseudo={t:{"sector":m["sector"],"fund_score":0.0} for t,m in meta.items()}
    benchdict={cfg["benchmark"]:benchmark}
    # enrich_signals expects key SPY; alias local benchmark.
    benchdict["SPY"]=benchmark
    for sec,s in sectors.items(): benchdict[f"SECTOR::{sec}"]=s

    # Custom minimal enrichment because standard enrich expects US sector ETF map.
    enriched=[]
    for ed,signals in prefund.items():
        for sig in signals:
            t=sig["ticker"]; df=prices[t]; ind=inds[t]
            try:i=ind["dates"].index(sig["signal_date"])
            except ValueError:continue
            closes=np.asarray(ind["close"],float); vols=pd.to_numeric(df["Volume"],errors="coerce").to_numpy(float)
            sc=float(closes[i]); low60=np.nanmin(closes[max(0,i-59):i+1]); high60=np.nanmax(closes[max(0,i-59):i+1])
            rng=(sc-low60)/(high60-low60) if high60>low60 else np.nan
            v5=np.nanmean(vols[max(0,i-4):i+1]); v60=np.nanmean(vols[max(0,i-59):i+1])
            vr=v5/v60 if v60>0 else np.nan
            gp=(float(sig["entry_price"])/sc-1)*100
            enriched.append({**sig,"signal_close":sc,"range_pos60":rng,"volume_ratio_5_60":vr,"gap_pct":gp})

    abc=[s for s in enriched if passes_abc(s)]
    cand=set(s["ticker"] for s in abc)
    scored=score_candidates(cand,meta,sp500_medians) if cand else pd.DataFrame()
    smap={r["ticker"]:float(r["fund_score"]) for _,r in scored.iterrows()} if len(scored) else {}
    full=[]
    for s in abc:
        fs=smap.get(s["ticker"],np.nan)
        if np.isfinite(fs) and fs>=6.5:
            x=dict(s);x["fund_score"]=fs;full.append(x)

    pmaps,dates=make_price_maps(prices)
    trades,skipped=build_episode_dataset(full,pmaps)
    em=episode_metrics(trades)
    pm,ptrades,eq=simulate_configuration(to_entries_by_date(full),pmaps,dates,1.5,4.5)
    return {
        "market":market,"constituents_scraped":len(uni),"prices_downloaded":len(prices),
        "candidate_tickers":len(cand),"abc_prefund":len(abc),"abc_full":len(full),
        "counts":counts,"episode_metrics":em,"portfolio_metrics":pm,
        "portfolio_trades":len(ptrades),"same_ticker_overlap_skipped":skipped,
    }

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    master=sorted(Path("data/master").glob("sp500_full_export_*.csv"))
    if not master: raise RuntimeError("Missing S&P500 master")
    sp=pd.read_csv(master[-1]); medians=_sector_medians(sp)

    results={}
    for market,cfg in MARKETS.items():
        print(f"\n=== {market} ===")
        try:results[market]=run_market(market,cfg,medians)
        except Exception as e:
            results[market]={"market":market,"error":repr(e)}
            print("ERROR",market,e)

    rows=[]
    for m,r in results.items():
        if "error" in r: continue
        em=r["episode_metrics"]; pm=r["portfolio_metrics"]
        rows.append({
            "market":m,"constituents":r["constituents_scraped"],"prices":r["prices_downloaded"],
            "abc_full":r["abc_full"],"episodes":em.get("n"),"win_rate":em.get("win_rate"),
            "profit_factor":em.get("profit_factor"),"avg_r":em.get("avg_r"),
            "portfolio_trades":pm.get("trades"),"return_pct":pm.get("total_return_pct"),
            "cagr_pct":pm.get("cagr_pct"),"mdd_pct":pm.get("max_drawdown_pct"),
            "calmar":pm.get("calmar"),"sharpe":pm.get("sharpe")
        })
    pd.DataFrame(rows).to_csv(OUT/"abc_cross_market_summary.csv",index=False)
    report={
        "generated_at":datetime.utcnow().isoformat()+"Z",
        "strategy_version":STRATEGY_VERSION,"model_version":MODEL_VERSION,
        "methodology":{
            "years":YEARS,
            "abc":"Frozen A/B/C thresholds from US discovery",
            "market_gate":"local benchmark 20d <= +1%",
            "abnormal":"stock residual vs local market + equal-weight local sector factor, 120d train + 20d recent",
            "fund_score":"current fundamentals scored against current S&P500 sector medians; proxy applied retrospectively",
            "portfolio":"1.5% trade risk / 4.5% aggregate / 3 positions",
            "survivorship":"current index constituents; known survivorship bias",
        },
        "results":results
    }
    def clean(o):
        if isinstance(o,dict):return {str(k):clean(v) for k,v in o.items()}
        if isinstance(o,list):return [clean(v) for v in o]
        if isinstance(o,(np.floating,)):return None if not np.isfinite(o) else float(o)
        if isinstance(o,(np.integer,)):return int(o)
        if isinstance(o,float) and not np.isfinite(o):return None
        return o
    (OUT/"abc_cross_market_report.json").write_text(json.dumps(clean(report),ensure_ascii=False,indent=2),encoding="utf-8")
    print(pd.DataFrame(rows).to_string(index=False))

if __name__=="__main__":
    main()
