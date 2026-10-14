#!/usr/bin/env python3
"""
SIDI cross-market stabilization component study.

Tests BASE, A, B, C, AB, AC, BC, ABC on the same non-US markets,
with all thresholds frozen from the US discovery.

A = range_pos60 >= 0.088
B = gap_pct >= -0.17
C = 0.848 <= volume_ratio_5_60 < 1.0
"""
from __future__ import annotations

import io, json, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from lxml import html as lxml_html

from backtest import build_indicators
from modules.ingesta.fundamental import _fund_yf
from modules.ingesta.scoring import _fund_score, _sector_medians
from modules.ingesta.sidi_context import _abnormal20, _close_series, _spy20
from pattern_backtest import build_episode_dataset, metrics as episode_metrics
from risk_matrix_backtest import make_price_maps, simulate_configuration
from stabilization_gate_backtest import (
    A_RANGE_POS60_MIN, B_GAP_PCT_MIN, C_VOL_RATIO_MIN, C_VOL_RATIO_MAX,
    to_entries_by_date,
)

MODEL_VERSION="SIDI_CROSS_MARKET_COMPONENTS_V2"  # normalized-price portfolio sizing
STRATEGY_VERSION="SIDI_INTRADAY_V2"
YEARS=8
OUT=Path("data/cross_market_components")

MARKETS={
    "FTSE100":{"url":"https://en.wikipedia.org/wiki/FTSE_100_Index","benchmark":"^FTSE","suffix":".L","min_rows":80},
    "NIKKEI225":{"url":"https://en.wikipedia.org/wiki/Nikkei_225","benchmark":"^N225","suffix":".T","min_rows":150},
    "TSX60":{"url":"https://en.wikipedia.org/wiki/S%26P/TSX_60","benchmark":"^GSPTSE","suffix":".TO","min_rows":40},
    "ASX200":{"url":"https://en.wikipedia.org/wiki/S%26P/ASX_200","benchmark":"^AXJO","suffix":".AX","min_rows":150},
}

CONFIGS={
    "BASE":(),
    "A":("A",),
    "B":("B",),
    "C":("C",),
    "AB":("A","B"),
    "AC":("A","C"),
    "BC":("B","C"),
    "ABC":("A","B","C"),
}

def fetch_nikkei(cfg):
    r=requests.get(cfg["url"],headers={"User-Agent":"Mozilla/5.0 SIDI"},timeout=45)
    r.raise_for_status()
    root=lxml_html.fromstring(r.content)
    rows=[]
    current_sector="Unknown"
    # Wikipedia currently renders components as H3 sections with LI children.
    for el in root.xpath("//h3|//li"):
        tag=el.tag.lower() if isinstance(el.tag,str) else ""
        if tag=="h3":
            txt=" ".join(el.text_content().split())
            if txt and txt.lower() not in {"edit"}:
                current_sector=re.sub(r"\[edit\]$","",txt,flags=re.I).strip()
        elif tag=="li":
            txt=" ".join(el.text_content().split())
            if "TYO" not in txt:
                continue
            code=None
            for a in el.xpath(".//a"):
                at=" ".join(a.text_content().split()).strip()
                if re.fullmatch(r"[0-9A-Z]{4}",at or ""):
                    code=at
            if not code:
                m=re.search(r"TYO\s*:\s*([0-9A-Z]{4})",txt)
                code=m.group(1) if m else None
            if not code:
                continue
            links=[" ".join(a.text_content().split()).strip() for a in el.xpath(".//a")]
            name=next((x for x in links if x and x not in {"TYO",code} and not re.fullmatch(r"[0-9A-Z]{4}",x)),code)
            rows.append({"raw_ticker":code,"ticker":code+cfg["suffix"],"name":name,"sector":current_sector,"market":"NIKKEI225"})
    df=pd.DataFrame(rows).drop_duplicates("ticker")
    if len(df)<180:
        raise RuntimeError(f"Nikkei parser found only {len(df)} constituents")
    return df

def fetch_table(market,cfg):
    if market=="NIKKEI225":
        return fetch_nikkei(cfg)
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
    if best is None: raise RuntimeError(f"No constituent table for {market}")
    cmap={}
    for c in best.columns:
        cl=c.lower()
        if any(k in cl for k in ["ticker","symbol","code","epic"]): cmap[c]="raw_ticker"
        elif any(k in cl for k in ["company","constituent","security"]): cmap[c]="name"
        elif "sector" in cl or "industry" in cl: cmap[c]="sector"
    best=best.rename(columns=cmap)
    if "sector" not in best: best["sector"]="Unknown"
    best=best[["raw_ticker","name","sector"]].copy()
    best["raw_ticker"]=best["raw_ticker"].astype(str).str.strip()
    best["ticker"]=best["raw_ticker"].str.replace(".","-",regex=False)+cfg["suffix"]
    best["market"]=market
    return best.dropna(subset=["ticker"]).drop_duplicates("ticker")

def download_prices(tickers,years=YEARS):
    end=datetime.today().strftime("%Y-%m-%d")
    start=(datetime.today()-timedelta(days=365*years+30)).strftime("%Y-%m-%d")
    out={}
    for i in range(0,len(tickers),40):
        chunk=tickers[i:i+40]
        raw=None
        for attempt in range(2):
            try:
                raw=yf.download(chunk,start=start,end=end,auto_adjust=True,progress=False,group_by="ticker",threads=True)
                if raw is not None and not raw.empty: break
            except Exception: pass
            time.sleep(1.5)
        if raw is None or raw.empty: continue
        for t in chunk:
            try:
                d=raw[t].copy() if isinstance(raw.columns,pd.MultiIndex) else raw.copy()
                d=d.reset_index().dropna(subset=["Close"])
                if len(d)>170: out[t]=d.reset_index(drop=True)
            except Exception: pass
        time.sleep(.35)
    return out

def sector_indices(prices,meta):
    frames=[]
    for t,df in prices.items():
        sec=meta.get(t,{}).get("sector","Unknown")
        s=_close_series(df).pct_change()
        z=pd.DataFrame({"date":s.index,"ret":s.values,"sector":sec})
        frames.append(z)
    allr=pd.concat(frames,ignore_index=True)
    out={}
    for sec,g in allr.groupby("sector"):
        s=g.groupby("date")["ret"].mean().dropna()
        out[str(sec)]=(1+s).cumprod()*100
    return out

def build_prefund(prices,inds,meta,benchmark,sectors):
    entries={}
    counts={"technical":0,"context":0,"prefund":0}
    for t,ind in inds.items():
        if t not in meta: continue
        sec=meta[t]["sector"]; secidx=sectors.get(sec,pd.Series(dtype=float))
        stock=_close_series(prices[t]); frame=prices[t].copy().reset_index(drop=True)
        idx={str(r["Date"])[:10]:i for i,r in frame.iterrows()}
        for i in range(62,len(ind["dates"])-1):
            rsi,dd=ind["rsi"][i],ind["dd60"][i]
            hp,hc=ind["hist"][i-1],ind["hist"][i]
            vd,atr=ind["vdec"][i],ind["atr"][i]
            if not (pd.notna(rsi) and rsi<40 and pd.notna(dd) and dd<=-12 and pd.notna(hp) and pd.notna(hc) and hc>hp and bool(vd) and pd.notna(atr) and atr>0):
                continue
            counts["technical"]+=1
            sd=ind["dates"][i]; cut=pd.Timestamp(sd).tz_localize(None).normalize()
            m20=_spy20(benchmark.loc[:cut])
            ab,bs,bc,_=_abnormal20(stock.loc[:cut],benchmark.loc[:cut],secidx.loc[:cut]) if len(secidx) else (np.nan,np.nan,np.nan,0)
            if not (np.isfinite(m20) and np.isfinite(ab)): continue
            counts["context"]+=1
            if m20>1 or ab>-10: continue
            ri=idx.get(sd)
            if ri is None or ri+1>=len(frame): continue
            nxt=frame.iloc[ri+1]; ep=float(nxt["Open"])
            if not np.isfinite(ep) or ep<=0: continue
            entries.setdefault(str(nxt["Date"])[:10],[]).append({
                "ticker":t,"signal_date":sd,"entry_date":str(nxt["Date"])[:10],
                "entry_price":ep,"atr":float(atr),"fund_score":0.0,
                "dd60":float(dd),"rsi":float(rsi),"spy20":float(m20),
                "abnormal20":float(ab),"sector":sec,"market":meta[t]["market"],
                "beta_spy":float(bs),"beta_sector":float(bc),
            })
            counts["prefund"]+=1
    return entries,counts

def enrich(entries,prices,inds):
    out=[]
    for _,signals in entries.items():
        for s in signals:
            t=s["ticker"]; ind=inds[t]; df=prices[t].reset_index(drop=True)
            try:i=ind["dates"].index(s["signal_date"])
            except ValueError: continue
            close=np.asarray(ind["close"],float); vols=pd.to_numeric(df["Volume"],errors="coerce").to_numpy(float)
            sc=float(close[i]); lo=np.nanmin(close[max(0,i-59):i+1]); hi=np.nanmax(close[max(0,i-59):i+1])
            rpos=(sc-lo)/(hi-lo) if hi>lo else np.nan
            v5=np.nanmean(vols[max(0,i-4):i+1]); v60=np.nanmean(vols[max(0,i-59):i+1])
            vr=v5/v60 if v60>0 else np.nan
            gap=(float(s["entry_price"])/sc-1)*100
            out.append({**s,"signal_close":sc,"range_pos60":rpos,"volume_ratio_5_60":vr,"gap_pct":gap})
    return out

def score_candidates(tickers,meta,medians):
    rows=[]
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs={ex.submit(_fund_yf,t):t for t in sorted(tickers)}
        for fut in as_completed(futs):
            t=futs[fut]
            try:d=fut.result()
            except Exception:d={}
            d["ticker"]=t; d["sector"]=meta[t]["sector"]; rows.append(d)
    out=[]
    for d in rows:
        sc=_fund_score(pd.Series(d),medians)
        z=dict(d); z.update(sc.to_dict()); out.append(z)
    return pd.DataFrame(out)

def flags(s):
    a=float(s.get("range_pos60",np.nan)); b=float(s.get("gap_pct",np.nan)); c=float(s.get("volume_ratio_5_60",np.nan))
    return {
        "A":np.isfinite(a) and a>=A_RANGE_POS60_MIN,
        "B":np.isfinite(b) and b>=B_GAP_PCT_MIN,
        "C":np.isfinite(c) and C_VOL_RATIO_MIN<=c<C_VOL_RATIO_MAX,
    }

def passes(s,config):
    f=flags(s)
    return all(f[x] for x in CONFIGS[config])


def normalize_price_space(prices, signals):
    """
    Normalize each local-currency price series to synthetic base 100.
    This preserves percentage returns, gaps and all R-multiples while
    removing currency-unit distortions from cash/notional sizing.
    """
    multipliers={}
    normalized_prices={}
    for t,df in prices.items():
        z=df.copy()
        close=pd.to_numeric(z["Close"],errors="coerce").dropna()
        if len(close)==0 or float(close.iloc[0])<=0:
            continue
        m=100.0/float(close.iloc[0])
        multipliers[t]=m
        for col in ["Open","High","Low","Close"]:
            if col in z.columns:
                z[col]=pd.to_numeric(z[col],errors="coerce")*m
        normalized_prices[t]=z
    normalized_signals=[]
    for s in signals:
        m=multipliers.get(s["ticker"])
        if m is None:
            continue
        x=dict(s)
        x["entry_price"]=float(x["entry_price"])*m
        x["atr"]=float(x["atr"])*m
        normalized_signals.append(x)
    return normalized_prices, normalized_signals


def run_config(full,config,pmaps,dates):
    sigs=[s for s in full if passes(s,config)]
    trades,sk=build_episode_dataset(sigs,pmaps)
    em=episode_metrics(trades)
    pm,ptrades,eq=simulate_configuration(to_entries_by_date(sigs),pmaps,dates,1.5,4.5)
    return {
        "signals":len(sigs),"episodes":em.get("n"),"episode_metrics":em,
        "portfolio_metrics":pm,"portfolio_trades":len(ptrades),"skipped_overlap":sk,
    }

def run_market(market,cfg,medians):
    uni=fetch_table(market,cfg); meta=uni.set_index("ticker").to_dict("index")
    prices=download_prices(uni["ticker"].tolist())
    bprices=download_prices([cfg["benchmark"]])
    if cfg["benchmark"] not in bprices: raise RuntimeError("benchmark download failed")
    benchmark=_close_series(bprices[cfg["benchmark"]])
    inds=build_indicators(prices); sectors=sector_indices(prices,meta)
    prefund,counts=build_prefund(prices,inds,meta,benchmark,sectors)
    enriched=enrich(prefund,prices,inds)
    candidates=set(s["ticker"] for s in enriched)
    scored=score_candidates(candidates,meta,medians) if candidates else pd.DataFrame()
    smap={r["ticker"]:float(r["fund_score"]) for _,r in scored.iterrows()} if len(scored) else {}
    full=[]
    for s in enriched:
        fs=smap.get(s["ticker"],np.nan)
        if np.isfinite(fs) and fs>=6.5:
            x=dict(s);x["fund_score"]=fs;full.append(x)
    normalized_prices, normalized_full = normalize_price_space(prices, full)
    pmaps,dates=make_price_maps(normalized_prices)
    results={cfg_name:run_config(normalized_full,cfg_name,pmaps,dates) for cfg_name in CONFIGS}
    return {
        "market":market,"constituents":len(uni),"prices":len(prices),
        "prefund_counts":counts,"full_before_stabilization":len(full),
        "candidate_tickers":len(candidates),"results":results,
    }

def clean(o):
    if isinstance(o,dict): return {str(k):clean(v) for k,v in o.items()}
    if isinstance(o,list): return [clean(v) for v in o]
    if isinstance(o,(np.integer,)): return int(o)
    if isinstance(o,(np.floating,)): return None if not np.isfinite(o) else float(o)
    if isinstance(o,float) and not np.isfinite(o): return None
    return o

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    master=sorted(Path("data/master").glob("sp500_full_export_*.csv"))
    if not master: raise RuntimeError("missing S&P500 master")
    medians=_sector_medians(pd.read_csv(master[-1]))

    report={"generated_at":datetime.utcnow().isoformat()+"Z","model_version":MODEL_VERSION,"strategy_version":STRATEGY_VERSION,"markets":{}}
    rows=[]
    for market,cfg in MARKETS.items():
        print(f"\n=== {market} ===")
        try:
            r=run_market(market,cfg,medians); report["markets"][market]=r
            base=r["results"]["BASE"]
            for config,res in r["results"].items():
                em=res["episode_metrics"]; pm=res["portfolio_metrics"]
                rows.append({
                    "market":market,"config":config,
                    "signals":res["signals"],"episodes":em.get("n"),
                    "win_rate":em.get("win_rate"),"profit_factor":em.get("profit_factor"),
                    "avg_r":em.get("avg_r"),"hard_loss_rate":em.get("hard_loss_rate"),
                    "portfolio_trades":pm.get("trades"),"return_pct":pm.get("total_return_pct"),
                    "cagr_pct":pm.get("cagr_pct"),"mdd_pct":pm.get("max_drawdown_pct"),
                    "calmar":pm.get("calmar"),"sharpe":pm.get("sharpe"),
                    "delta_avg_r_vs_base":(em.get("avg_r")-base["episode_metrics"].get("avg_r")) if em.get("avg_r") is not None and base["episode_metrics"].get("avg_r") is not None else np.nan,
                    "delta_pf_vs_base":(em.get("profit_factor")-base["episode_metrics"].get("profit_factor")) if em.get("profit_factor") is not None and base["episode_metrics"].get("profit_factor") is not None else np.nan,
                })
        except Exception as e:
            report["markets"][market]={"error":repr(e)}
            print("ERROR",market,e)
    df=pd.DataFrame(rows)
    df.to_csv(OUT/"cross_market_components_summary.csv",index=False)
    report["methodology"]={
        "configs":list(CONFIGS),"years":YEARS,
        "A":f"range_pos60 >= {A_RANGE_POS60_MIN}",
        "B":f"gap_pct >= {B_GAP_PCT_MIN}",
        "C":f"{C_VOL_RATIO_MIN} <= volume_ratio_5_60 < {C_VOL_RATIO_MAX}",
        "market_context":"local benchmark + equal-weight local sector factor",
        "fund_score":"current fundamentals scored against S&P500 sector medians; retrospective proxy",
        "portfolio":"1.5% / 4.5% / max 3; local price series normalized to base 100 before sizing",
    }
    (OUT/"cross_market_components_report.json").write_text(json.dumps(clean(report),ensure_ascii=False,indent=2),encoding="utf-8")
    print("\nSUMMARY")
    print(df.to_string(index=False))

if __name__=="__main__":
    main()
