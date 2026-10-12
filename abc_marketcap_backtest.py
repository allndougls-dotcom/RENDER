#!/usr/bin/env python3
"""
SIDI_INTRADAY_V2 · ABC Market-Cap Threshold Backtest

Uses the already validated expanded-universe ABC signal set and tests
current-market-cap thresholds as a proxy for "large cap quality":

ALL, >=5B, >=10B, >=20B, >=50B, >=100B

Frozen:
- ABC gate
- fund_score >= 6.5
- portfolio risk 1.5% / 4.5% aggregate / max 3 positions
- execution and exit rules from SIDI_INTRADAY_V2

Known limitation:
market cap is current and applied retroactively. This is a classification
proxy, not point-in-time market cap.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from abc_expanded_universe_backtest import fast_download_prices
from pattern_backtest import build_episode_dataset, metrics as episode_metrics
from risk_matrix_backtest import make_price_maps, simulate_configuration
from stabilization_gate_backtest import to_entries_by_date

STRATEGY_VERSION = "SIDI_INTRADAY_V2"
MODEL_VERSION = "SIDI_ABC_MARKETCAP_V1"
TRADE_RISK_PCT = 1.5
PORTFOLIO_CAP_PCT = 4.5
YEARS = 8
OUT_DIR = Path("data/abc_marketcap")

THRESHOLDS = [
    ("ALL_SP1500", 0),
    ("MCAP_5B", 5_000_000_000),
    ("MCAP_10B", 10_000_000_000),
    ("MCAP_20B", 20_000_000_000),
    ("MCAP_50B", 50_000_000_000),
    ("MCAP_100B", 100_000_000_000),
]


def safe(v, default=np.nan):
    try:
        x=float(v)
        return x if np.isfinite(x) else default
    except Exception:
        return default


def load_data():
    sig_path=Path("data/abc_expanded_universe/abc_expanded_signals.csv")
    fund_path=Path("data/abc_expanded_universe/abc_candidate_fundamentals.csv")
    if not sig_path.exists() or not fund_path.exists():
        raise RuntimeError("Missing expanded ABC source files")

    sig=pd.read_csv(sig_path)
    fund=pd.read_csv(fund_path)
    fund=fund[["ticker","market_cap"]].drop_duplicates("ticker")
    merged=sig.merge(fund,on="ticker",how="left")
    merged["market_cap"]=pd.to_numeric(merged["market_cap"],errors="coerce")
    missing=int(merged["market_cap"].isna().sum())
    print(f"Signals: {len(merged)} · missing market cap: {missing}")
    return merged


def records(df):
    # Convert NaN to None-like values only where downstream tolerates them.
    out=[]
    for rec in df.to_dict("records"):
        clean={}
        for k,v in rec.items():
            if isinstance(v,float) and not np.isfinite(v):
                clean[k]=np.nan
            else:
                clean[k]=v
        out.append(clean)
    return out


def run_group(label, df, price_maps, all_dates):
    sigs=records(df)
    episodes, skipped=build_episode_dataset(sigs,price_maps)
    em=episode_metrics(episodes)

    entries=to_entries_by_date(sigs)
    pm, ptrades, eq=simulate_configuration(
        entries, price_maps, all_dates, TRADE_RISK_PCT, PORTFOLIO_CAP_PCT
    )

    return {
        "label":label,
        "signals":len(sigs),
        "unique_tickers":int(df["ticker"].nunique()),
        "episodes":int(em.get("n",0)),
        "episode_metrics":em,
        "portfolio_metrics":pm,
        "same_ticker_overlap_skipped":skipped,
        "portfolio_trades":len(ptrades),
        "index_mix":df["index_bucket"].value_counts().to_dict(),
        "median_market_cap_b":safe(df["market_cap"].median()/1e9),
        "p25_market_cap_b":safe(df["market_cap"].quantile(.25)/1e9),
        "p75_market_cap_b":safe(df["market_cap"].quantile(.75)/1e9),
    }


def annual_metrics(df, price_maps):
    rows=[]
    for label,threshold in THRESHOLDS:
        g=df[df["market_cap"]>=threshold].copy() if threshold>0 else df.copy()
        for year in sorted(g["entry_date"].astype(str).str[:4].unique()):
            sub=g[g["entry_date"].astype(str).str[:4]==year]
            if len(sub)==0:
                continue
            t,_=build_episode_dataset(records(sub),price_maps)
            m=episode_metrics(t)
            rows.append({
                "group":label,
                "year":int(year),
                "signals":len(sub),
                "episodes":m.get("n"),
                "win_rate":m.get("win_rate"),
                "avg_r":m.get("avg_r"),
                "profit_factor":m.get("profit_factor"),
                "hard_loss_rate":m.get("hard_loss_rate"),
            })
    return pd.DataFrame(rows)


def robustness(annual):
    rows=[]
    base=annual[annual["group"]=="ALL_SP1500"].set_index("year")
    for label,_ in THRESHOLDS:
        if label=="ALL_SP1500":
            continue
        sub=annual[annual["group"]==label].set_index("year")
        common=sorted(set(base.index)&set(sub.index))
        if not common:
            continue
        pairs=[]
        for y in common:
            b=base.loc[y]
            s=sub.loc[y]
            pairs.append({
                "year":y,
                "delta_wr":safe(s["win_rate"])-safe(b["win_rate"]),
                "delta_avg_r":safe(s["avg_r"])-safe(b["avg_r"]),
                "delta_pf":safe(s["profit_factor"])-safe(b["profit_factor"]),
            })
        p=pd.DataFrame(pairs)
        rows.append({
            "group":label,
            "years":len(p),
            "pct_years_wr_improves":float((p["delta_wr"]>0).mean()*100),
            "pct_years_avg_r_improves":float((p["delta_avg_r"]>0).mean()*100),
            "pct_years_pf_improves":float((p["delta_pf"]>0).mean()*100),
            "median_delta_wr":float(p["delta_wr"].median()),
            "median_delta_avg_r":float(p["delta_avg_r"].median()),
            "median_delta_pf":float(p["delta_pf"].median()),
        })
    return pd.DataFrame(rows)


def json_clean(obj):
    if isinstance(obj,dict):
        return {str(k):json_clean(v) for k,v in obj.items()}
    if isinstance(obj,list):
        return [json_clean(v) for v in obj]
    if isinstance(obj,(np.integer,)):
        return int(obj)
    if isinstance(obj,(np.floating,)):
        return None if not np.isfinite(obj) else float(obj)
    if isinstance(obj,float) and not np.isfinite(obj):
        return None
    return obj


def main():
    OUT_DIR.mkdir(parents=True,exist_ok=True)
    df=load_data()
    tickers=sorted(df["ticker"].dropna().unique())
    prices=fast_download_prices(tickers,years=YEARS)
    price_maps,all_dates=make_price_maps(prices)

    results={}
    rows=[]
    for label,threshold in THRESHOLDS:
        g=df[df["market_cap"]>=threshold].copy() if threshold>0 else df.copy()
        # only tickers with price history
        g=g[g["ticker"].isin(prices)].copy()
        result=run_group(label,g,price_maps,all_dates)
        results[label]=result
        em=result["episode_metrics"]
        pm=result["portfolio_metrics"]
        rows.append({
            "group":label,
            "threshold_b":threshold/1e9,
            "signals":result["signals"],
            "unique_tickers":result["unique_tickers"],
            "episodes":em.get("n"),
            "win_rate":em.get("win_rate"),
            "avg_r":em.get("avg_r"),
            "profit_factor":em.get("profit_factor"),
            "hard_loss_rate":em.get("hard_loss_rate"),
            "gap_loss_rate":em.get("gap_loss_rate"),
            "portfolio_trades":pm.get("trades"),
            "portfolio_return_pct":pm.get("total_return_pct"),
            "portfolio_cagr_pct":pm.get("cagr_pct"),
            "portfolio_mdd_pct":pm.get("max_drawdown_pct"),
            "portfolio_calmar":pm.get("calmar"),
            "portfolio_sharpe":pm.get("sharpe"),
            "median_market_cap_b":result["median_market_cap_b"],
        })

    # Reference: actual S&P500 ABC, independent of market cap.
    sp500=df[df["index_bucket"]=="SP500"].copy()
    sp500=sp500[sp500["ticker"].isin(prices)]
    sp500_result=run_group("SP500_ABC_REFERENCE",sp500,price_maps,all_dates)
    results["SP500_ABC_REFERENCE"]=sp500_result
    em=sp500_result["episode_metrics"]; pm=sp500_result["portfolio_metrics"]
    rows.append({
        "group":"SP500_ABC_REFERENCE",
        "threshold_b":np.nan,
        "signals":sp500_result["signals"],
        "unique_tickers":sp500_result["unique_tickers"],
        "episodes":em.get("n"),
        "win_rate":em.get("win_rate"),
        "avg_r":em.get("avg_r"),
        "profit_factor":em.get("profit_factor"),
        "hard_loss_rate":em.get("hard_loss_rate"),
        "gap_loss_rate":em.get("gap_loss_rate"),
        "portfolio_trades":pm.get("trades"),
        "portfolio_return_pct":pm.get("total_return_pct"),
        "portfolio_cagr_pct":pm.get("cagr_pct"),
        "portfolio_mdd_pct":pm.get("max_drawdown_pct"),
        "portfolio_calmar":pm.get("calmar"),
        "portfolio_sharpe":pm.get("sharpe"),
        "median_market_cap_b":sp500_result["median_market_cap_b"],
    })

    summary=pd.DataFrame(rows)
    annual=annual_metrics(df[df["ticker"].isin(prices)],price_maps)
    robust=robustness(annual)

    summary.to_csv(OUT_DIR/"abc_marketcap_summary.csv",index=False)
    annual.to_csv(OUT_DIR/"abc_marketcap_annual.csv",index=False)
    robust.to_csv(OUT_DIR/"abc_marketcap_robustness.csv",index=False)

    # Rank by quality and by portfolio efficiency.
    ranked=summary[summary["group"].str.startswith("MCAP_")].copy()
    ranked["quality_score"]=(
        ranked["win_rate"].rank(pct=True)
        + ranked["profit_factor"].rank(pct=True)
        + ranked["avg_r"].rank(pct=True)
    )
    ranked["efficiency_score"]=(
        ranked["portfolio_calmar"].rank(pct=True)
        + ranked["portfolio_return_pct"].rank(pct=True)
    )
    ranked=ranked.sort_values(
        ["efficiency_score","quality_score"],ascending=False
    )

    report={
        "generated_at":datetime.utcnow().isoformat()+"Z",
        "strategy_version":STRATEGY_VERSION,
        "model_version":MODEL_VERSION,
        "methodology":{
            "source":"Previously validated 559 S&P1500 ABC full signals",
            "market_cap":"Current market cap applied retroactively (proxy limitation)",
            "thresholds_b":[0,5,10,20,50,100],
            "portfolio":{
                "trade_risk_pct":TRADE_RISK_PCT,
                "portfolio_risk_cap_pct":PORTFOLIO_CAP_PCT,
                "max_positions":3,
            },
        },
        "source_signals":len(df),
        "source_unique_tickers":int(df["ticker"].nunique()),
        "prices_downloaded":len(prices),
        "results":results,
        "ranked_market_cap_thresholds":ranked.to_dict("records"),
        "annual_robustness":robust.to_dict("records"),
    }
    (OUT_DIR/"abc_marketcap_report.json").write_text(
        json.dumps(json_clean(report),ensure_ascii=False,indent=2),encoding="utf-8"
    )

    print("\nMARKET-CAP RESULTS")
    cols=[
        "group","signals","episodes","win_rate","profit_factor","avg_r",
        "portfolio_return_pct","portfolio_cagr_pct","portfolio_mdd_pct",
        "portfolio_calmar"
    ]
    print(summary[cols].to_string(index=False))
    print("\nANNUAL ROBUSTNESS")
    print(robust.to_string(index=False))


if __name__=="__main__":
    main()
