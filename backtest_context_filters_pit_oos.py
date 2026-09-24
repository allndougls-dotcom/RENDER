"""Validate SIDI contextual filters with calibrated PIT fundamentals.

The context thresholds were discovered on 2024-08-11..2026-09-10 using the
current-score proxy. This script does NOT optimise them. It freezes:
- SPY 20d return <= +1%
- beta-adjusted abnormal 20d return <= -10%
- A+ additionally requires VIX percentile(252) >= 40

It then rebuilds the historical fundamental filter with the calibrated
quarterly/TTM PIT proxy >= 6.66 (coverage >= 6/8) and evaluates:
1) PRE_DISCOVERY_OOS: 2023-01-01..2024-08-10 (true contextual holdout)
2) DISCOVERY_PERIOD_PIT: 2024-08-11..2026-09-10 (same context sample, but PIT fundamentals)
3) RECENT_TEMPORAL: 2025-09-01..2026-09-10

Execution remains frozen: entry T+1 open, TP 0.75xATR, SL -5%, T7,
risk 1.5%, max 5 positions, 10 bps round-trip costs.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import backtest as bt
import backtest_experiments as exp
import validate_sidi_candidate as val
import historical_membership as membership
import calibrate_pit_proxy_ttm as cal_ttm
import pit_proxy_ttm_fast as ttm_fast
import yfinance_pit_proxy as pit_base
import analyze_signal_context_features as feat
import backtest_context_filters as ctxmod
from modules.ingesta.scoring import _sector_medians, _fund_score

SIGNAL_START = "2023-01-01"
END = "2026-09-10"
PRICE_START = "2022-01-01"
PROXY_THRESHOLD = 6.66
MIN_COVERAGE = 6
COST_BPS = 10

CANDIDATE = exp.Experiment(
    "PIT_CONTEXT_ATR075_T7",
    target_mode="atr", atr_mult=0.75,
    stop_mode="fixed", stop_pct=0.05,
    time_stop=7,
    description="PIT>=6.66 / DD12 / TP0.75ATR / SL5 / T7",
)

WINDOWS = [
    ("PRE_DISCOVERY_OOS", "2023-01-01", "2024-08-10"),
    ("DISCOVERY_PERIOD_PIT", "2024-08-11", "2026-09-10"),
    ("RECENT_TEMPORAL", "2025-09-01", "2026-09-10"),
]


def price_maps(prices: dict[str, pd.DataFrame]) -> dict[str, dict[str, float]]:
    out = {}
    for ticker, df in prices.items():
        out[ticker] = {
            str(r.Date)[:10]: float(r.Close)
            for r in df[["Date", "Close"]].itertuples(index=False)
            if pd.notna(r.Close)
        }
    return out


def dynamic_scores(signal_dates, tickers, sectors, state_cache, prices_by_date, added):
    score_by_date = {}
    coverage_rows = []
    n = len(signal_dates)
    for i, asof in enumerate(signal_dates, 1):
        active = [
            t for t in tickers
            if t in state_cache and (added.get(t) is None or asof >= added[t])
        ]
        rows = []
        for ticker in active:
            price = prices_by_date.get(ticker, {}).get(asof, np.nan)
            rows.append(ttm_fast.row_asof(
                state_cache, ticker, sectors.get(ticker, "Unknown"), asof, price
            ))
        df = pd.DataFrame(rows)
        if df.empty:
            score_by_date[asof] = {}
            coverage_rows.append({"date": asof, "active": 0, "mean_coverage": 0.0,
                                  "coverage_ge6_pct": 0.0, "score_ge666": 0})
            continue
        sm = _sector_medians(df)
        scored = df.apply(lambda r: _fund_score(r, sm), axis=1)
        df = pd.concat([df, scored], axis=1)
        score_by_date[asof] = {
            str(r.ticker): {"fund_score": float(r.fund_score), "coverage": int(r.metric_coverage)}
            for r in df.itertuples()
        }
        coverage_rows.append({
            "date": asof,
            "active": len(active),
            "mean_coverage": float(df["metric_coverage"].mean()),
            "coverage_ge6_pct": float((df["metric_coverage"] >= MIN_COVERAGE).mean()*100),
            "score_ge666": int((df["fund_score"] >= PROXY_THRESHOLD).sum()),
        })
        if i % 50 == 0 or i == n:
            c = coverage_rows[-1]
            print(f"  PIT {i}/{n} {asof}: coverage={c['mean_coverage']:.2f}/8 "
                  f">=6={c['coverage_ge6_pct']:.1f}% score>=6.66={c['score_ge666']}")
    return score_by_date, pd.DataFrame(coverage_rows)


def filter_pit(tech_map, scores):
    out = {}
    st = {"technical":0,"missing_score":0,"low_coverage":0,"below_threshold":0,"kept":0}
    for ticker, sigs in tech_map.items():
        for d, sig in sigs.items():
            st["technical"] += 1
            rec = scores.get(d, {}).get(ticker)
            if rec is None:
                st["missing_score"] += 1; continue
            if rec["coverage"] < MIN_COVERAGE:
                st["low_coverage"] += 1; continue
            if rec["fund_score"] < PROXY_THRESHOLD:
                st["below_threshold"] += 1; continue
            item = dict(sig)
            item["pit_fund_score"] = rec["fund_score"]
            item["pit_coverage"] = rec["coverage"]
            out.setdefault(ticker, {})[d] = item
            st["kept"] += 1
    return out, st


def present(x):
    return x is not None and pd.notna(x)


def rules():
    return {
        "PIT_BASE": lambda r: True,
        "PIT_SPY1_ABN_M10": lambda r: (
            present(r.get("spy20")) and present(r.get("abnormal20"))
            and r["spy20"] <= 1.0 and r["abnormal20"] <= -10.0
        ),
        "PIT_A_PLUS": lambda r: (
            present(r.get("spy20")) and present(r.get("abnormal20")) and present(r.get("vix_pct"))
            and r["spy20"] <= 1.0 and r["abnormal20"] <= -10.0 and r["vix_pct"] >= 40.0
        ),
    }


def main():
    print("="*118)
    print("SIDI CONTEXT FILTERS — CALIBRATED PIT + PRE-DISCOVERY OOS")
    print("="*118)
    print("Frozen thresholds only. No optimisation in this run.\n")

    master, sectors = feat.load_master()
    tickers = bt.load_tickers()
    fund_scores = bt.load_fundamental_scores()
    added = membership.load_date_added()

    prices = bt.download_prices(tickers, start_date=PRICE_START, end_date=END)
    indicators = bt.build_indicators(prices)
    prices_idx = {t: feat.to_indexed(d) for t,d in prices.items()}
    pmap = price_maps(prices)

    spec = exp.signal_variant(12.0, False)
    spec["min_fund_score"] = 0.0
    raw, nraw = bt.build_signal_map(
        indicators, spec, spy_dict={}, date_from=SIGNAL_START, date_to=END,
        fund_scores=fund_scores, sector_regime=None,
    )
    tech_map, member_stats = membership.filter_signal_map(raw, added)
    signal_dates = sorted({d for sigs in tech_map.values() for d in sigs})
    print(f"Technical raw={nraw}; membership-kept={member_stats['kept']}; removed={member_stats['removed_pre_membership']}; dates={len(signal_dates)}")

    print("\nBuilding quarterly/TTM PIT fundamentals...")
    statements, failed = cal_ttm.fetch_full_statements(tickers)
    state_cache = ttm_fast.build_state_cache(statements, SIGNAL_START, END)
    scores, coverage = dynamic_scores(signal_dates, tickers, sectors, state_cache, pmap, added)
    pit_map, pit_stats = filter_pit(tech_map, scores)
    print("PIT filter:", json.dumps(pit_stats, indent=2))

    # References need a full year of pre-2023 history for 120d beta estimation + 20d recent window.
    feat.DOWNLOAD_START = PRICE_START
    refs = {s: feat.download_reference(s) for s in sorted(set(feat.SECTOR_ETF.values()) | {"SPY", "^VIX"})}
    print("Computing breadth/context on PIT-eligible signals...")
    market_breadth, sector_breadth = feat.breadth_maps(prices_idx, sectors, added)
    context = ctxmod.build_context(pit_map, prices_idx, sectors, refs, market_breadth, sector_breadth)

    maps = {}
    full_rows = []
    window_rows = []
    for name, pred in rules().items():
        smap, nkept = ctxmod.subset_signal_map(pit_map, context, pred)
        maps[name] = smap
        st, _ = val.run_sim(smap, prices, indicators, fund_scores, CANDIDATE,
                            SIGNAL_START, END, cost_bps=COST_BPS)
        st.update({"filter":name,"signals_kept":nkept,"pit_signals":pit_stats["kept"],
                   "signal_keep_pct":100*nkept/pit_stats["kept"] if pit_stats["kept"] else 0})
        full_rows.append(st)
        print(f"FULL {name:<20} sig={nkept:4d} trades={st['trades']:3d} WR={st['win_rate']:6.2f}% "
              f"PF={st['profit_factor']:5.2f} Ret={st['total_return']:8.2f}% MDD={st['max_drawdown_mtm']:7.2f}%")

    print("\nWINDOWS — capital reset")
    print("-"*118)
    for wname, ws, we in WINDOWS:
        for name, smap in maps.items():
            st, _ = val.run_sim(smap, prices, indicators, fund_scores, CANDIDATE,
                                ws, we, cost_bps=COST_BPS)
            st.update({"window":wname,"filter":name})
            window_rows.append(st)
            print(f"{wname:<22} {name:<20} trades={st['trades']:3d} WR={st['win_rate']:6.2f}% "
                  f"PF={st['profit_factor']:5.2f} Ret={st['total_return']:7.2f}% MDD={st['max_drawdown_mtm']:7.2f}%")

    cov = coverage.copy()
    if not cov.empty:
        cov["period"] = np.where(cov["date"] <= "2024-08-10", "PRE_DISCOVERY_OOS", "DISCOVERY_PERIOD_PIT")
        cov_summary = cov.groupby("period").agg(
            signal_dates=("date","size"), mean_coverage=("mean_coverage","mean"),
            coverage_ge6_pct=("coverage_ge6_pct","mean"), mean_score_ge666=("score_ge666","mean")
        ).reset_index()
    else:
        cov_summary = pd.DataFrame()

    pd.DataFrame(full_rows).drop(columns=["config"], errors="ignore").to_csv("sidi_context_pit_full.csv", index=False)
    pd.DataFrame(window_rows).drop(columns=["config"], errors="ignore").to_csv("sidi_context_pit_windows.csv", index=False)
    coverage.to_csv("sidi_context_pit_coverage.csv", index=False)
    cov_summary.to_csv("sidi_context_pit_coverage_summary.csv", index=False)
    Path("sidi_context_pit_results.json").write_text(json.dumps({
        "generated_at":datetime.utcnow().isoformat()+"Z",
        "frozen":{
            "pit_proxy_min":PROXY_THRESHOLD,"coverage_min":MIN_COVERAGE,
            "spy20_max_pct":1.0,"abnormal20_max_pct":-10.0,"a_plus_vix_percentile_min":40.0,
            "entry":"T+1 open","tp":"0.75xATR14","sl_pct":5.0,"time_stop":7,
            "risk_pct":1.5,"max_positions":5,"cost_bps_rt":COST_BPS,
        },
        "membership":member_stats,"pit_filter":pit_stats,"statement_failures":failed,
        "coverage_summary":cov_summary.to_dict("records"),
        "full":full_rows,"windows":window_rows,
        "limitations":[
            "yFinance historical statements may include later restatements.",
            "45d quarterly / 90d annual lags approximate filing availability.",
            "Removed historical S&P 500 constituents remain absent.",
            "PIT coverage is limited in early 2023 and inadequate before 2023; this run starts in 2023.",
            "Context thresholds were discovered on 2024-08-11..2026-09-10 and are not re-optimised here.",
        ],
    },indent=2,ensure_ascii=False,default=str),encoding="utf-8")
    print("\nSaved calibrated PIT context validation outputs.")

if __name__ == "__main__":
    main()
