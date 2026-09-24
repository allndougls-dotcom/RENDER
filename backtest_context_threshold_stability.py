"""Robustness test for SIDI contextual thresholds.

This is NOT an optimisation pass. The candidate context rule already validated was:
  SPY 20d return <= +1% AND beta-adjusted abnormal 20d return <= -10%.

We test a deliberately coarse 3x3 neighbourhood around that point:
  SPY20 max: 0%, +1%, +2%
  Abnormal20 max: -8%, -10%, -12%

The goal is to detect a plateau of acceptable performance, not select the best cell.
Fundamentals and execution stay frozen:
  PIT TTM proxy >= 6.66, coverage >= 6/8, DD60 >= 12%,
  entry T+1 open, TP 0.75x ATR14, SL -5%, time stop 7,
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
import analyze_signal_context_features as feat
import backtest_context_filters as ctxmod
import backtest_context_filters_pit_oos as pitmod

SIGNAL_START = "2023-01-01"
END = "2026-09-10"
PRICE_START = "2022-01-01"
COST_BPS = 10
SPY_THRESHOLDS = [0.0, 1.0, 2.0]
ABN_THRESHOLDS = [-8.0, -10.0, -12.0]

CANDIDATE = exp.Experiment(
    "PIT_CONTEXT_STABILITY",
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


def is_present(x):
    return x is not None and pd.notna(x)


def make_pred(spy_max: float, abn_max: float):
    return lambda r: (
        is_present(r.get("spy20")) and is_present(r.get("abnormal20"))
        and r["spy20"] <= spy_max and r["abnormal20"] <= abn_max
    )


def label(spy_max: float, abn_max: float) -> str:
    s = str(int(spy_max)) if float(spy_max).is_integer() else str(spy_max).replace(".", "p")
    a = str(abs(int(abn_max))) if float(abn_max).is_integer() else str(abs(abn_max)).replace(".", "p")
    return f"SPY_LE_{s}_ABN_LE_M{a}"


def run_one(smap, prices, indicators, fund_scores, start, end):
    st, _ = val.run_sim(
        smap, prices, indicators, fund_scores, CANDIDATE,
        start, end, cost_bps=COST_BPS,
    )
    return st


def plateau_summary(df: pd.DataFrame, window: str) -> dict:
    z = df[df["window"] == window].copy()
    if z.empty:
        return {}
    return {
        "window": window,
        "cells": int(len(z)),
        "profitable_cells": int((z["total_return"] > 0).sum()),
        "pf_gt_1_cells": int((z["profit_factor"] > 1).sum()),
        "pf_ge_1_3_cells": int((z["profit_factor"] >= 1.3).sum()),
        "wr_min": float(z["win_rate"].min()),
        "wr_median": float(z["win_rate"].median()),
        "wr_max": float(z["win_rate"].max()),
        "pf_min": float(z["profit_factor"].min()),
        "pf_median": float(z["profit_factor"].median()),
        "pf_max": float(z["profit_factor"].max()),
        "ret_min": float(z["total_return"].min()),
        "ret_median": float(z["total_return"].median()),
        "ret_max": float(z["total_return"].max()),
        "mdd_best": float(z["max_drawdown_mtm"].max()),
        "mdd_median": float(z["max_drawdown_mtm"].median()),
        "mdd_worst": float(z["max_drawdown_mtm"].min()),
        "trades_min": int(z["trades"].min()),
        "trades_median": float(z["trades"].median()),
        "trades_max": int(z["trades"].max()),
    }


def main():
    print("=" * 120)
    print("SIDI CONTEXT THRESHOLD STABILITY — COARSE 3x3 GRID")
    print("=" * 120)
    print("No threshold optimisation. Looking for a robustness plateau around SPY<=+1 / abnormal<=-10.\n")

    _, sectors = feat.load_master()
    tickers = bt.load_tickers()
    fund_scores = bt.load_fundamental_scores()
    added = membership.load_date_added()

    prices = bt.download_prices(tickers, start_date=PRICE_START, end_date=END)
    indicators = bt.build_indicators(prices)
    prices_idx = {t: feat.to_indexed(d) for t, d in prices.items()}
    pmap = pitmod.price_maps(prices)

    spec = exp.signal_variant(12.0, False)
    spec["min_fund_score"] = 0.0
    raw, nraw = bt.build_signal_map(
        indicators, spec, spy_dict={}, date_from=SIGNAL_START, date_to=END,
        fund_scores=fund_scores, sector_regime=None,
    )
    tech_map, member_stats = membership.filter_signal_map(raw, added)
    signal_dates = sorted({d for sigs in tech_map.values() for d in sigs})
    print(f"Technical raw={nraw}; membership-kept={member_stats['kept']}; removed={member_stats['removed_pre_membership']}; dates={len(signal_dates)}")

    print("\nBuilding calibrated quarterly/TTM PIT fundamentals...")
    statements, failed = cal_ttm.fetch_full_statements(tickers)
    state_cache = ttm_fast.build_state_cache(statements, SIGNAL_START, END)
    scores, coverage = pitmod.dynamic_scores(signal_dates, tickers, sectors, state_cache, pmap, added)
    pit_map, pit_stats = pitmod.filter_pit(tech_map, scores)
    print("PIT filter:", json.dumps(pit_stats, indent=2))

    feat.DOWNLOAD_START = PRICE_START
    refs = {s: feat.download_reference(s) for s in sorted(set(feat.SECTOR_ETF.values()) | {"SPY", "^VIX"})}
    print("Computing signal context...")
    market_breadth, sector_breadth = feat.breadth_maps(prices_idx, sectors, added)
    context = ctxmod.build_context(pit_map, prices_idx, sectors, refs, market_breadth, sector_breadth)

    rows = []
    maps = {}
    for spy_max in SPY_THRESHOLDS:
        for abn_max in ABN_THRESHOLDS:
            name = label(spy_max, abn_max)
            smap, nkept = ctxmod.subset_signal_map(pit_map, context, make_pred(spy_max, abn_max))
            maps[(spy_max, abn_max)] = smap
            for wname, ws, we in WINDOWS:
                st = run_one(smap, prices, indicators, fund_scores, ws, we)
                row = {
                    "window": wname,
                    "spy20_max": spy_max,
                    "abnormal20_max": abn_max,
                    "rule": name,
                    "signals_kept_full": nkept,
                    "pit_signals_full": pit_stats["kept"],
                    "signal_keep_pct_full": 100*nkept/pit_stats["kept"] if pit_stats["kept"] else 0.0,
                    **{k:v for k,v in st.items() if k != "config"},
                }
                rows.append(row)
                print(
                    f"{wname:<22} SPY<={spy_max:+.0f}% ABN<={abn_max:+.0f}% "
                    f"trades={st['trades']:3d} WR={st['win_rate']:6.2f}% PF={st['profit_factor']:5.2f} "
                    f"Ret={st['total_return']:7.2f}% MDD={st['max_drawdown_mtm']:7.2f}%"
                )

    df = pd.DataFrame(rows)
    df.to_csv("sidi_context_threshold_stability.csv", index=False)

    # Matrix outputs make the plateau visible without cherry-picking one cell.
    matrix_files = []
    for wname, _, _ in WINDOWS:
        sub = df[df["window"] == wname]
        for metric in ["profit_factor", "win_rate", "total_return", "max_drawdown_mtm", "trades"]:
            mat = sub.pivot(index="abnormal20_max", columns="spy20_max", values=metric).sort_index(ascending=False)
            fn = f"sidi_stability_{wname.lower()}_{metric}.csv"
            mat.to_csv(fn)
            matrix_files.append(fn)

    summaries = [plateau_summary(df, w[0]) for w in WINDOWS]
    print("\nPLATEAU SUMMARY")
    for s in summaries:
        print(json.dumps(s, indent=2))

    Path("sidi_context_threshold_stability.json").write_text(json.dumps({
        "generated_at": datetime.utcnow().isoformat()+"Z",
        "purpose": "Robustness plateau test, not threshold optimisation",
        "grid": {"spy20_max_pct": SPY_THRESHOLDS, "abnormal20_max_pct": ABN_THRESHOLDS},
        "frozen": {
            "pit_proxy_min": pitmod.PROXY_THRESHOLD,
            "coverage_min": pitmod.MIN_COVERAGE,
            "dd60_min_pct": 12.0,
            "entry": "T+1 open",
            "tp": "0.75xATR14",
            "sl_pct": 5.0,
            "time_stop": 7,
            "risk_pct": 1.5,
            "max_positions": 5,
            "cost_bps_rt": COST_BPS,
        },
        "membership": member_stats,
        "pit_filter": pit_stats,
        "statement_failures": failed,
        "plateau_summary": summaries,
        "rows": rows,
        "interpretation_rule": "Prefer a broad plateau; do not select a cell solely because it has the highest return/PF.",
        "limitations": [
            "Historical yFinance statements may include later restatements.",
            "45d quarterly / 90d annual lags approximate filing availability.",
            "Removed historical S&P 500 constituents remain absent.",
            "Early-2023 PIT fundamental coverage is incomplete.",
        ],
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    coverage.to_csv("sidi_context_threshold_stability_coverage.csv", index=False)
    print("\nSaved stability grid, matrices, coverage and JSON.")


if __name__ == "__main__":
    main()
