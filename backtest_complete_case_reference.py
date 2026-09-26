"""SIDI complete-case reference validation.

Pragmatic policy: do not reconstruct delisted/removed securities. Only evaluate
signals when every input required by the current frozen SIDI candidate is
available on the signal date.

Frozen candidate:
- current available universe only
- DD60 >= 12%, RSI<40, MACD histogram improving, declining volume
- calibrated quarterly/TTM PIT proxy >= 6.66, metric coverage >= 6/8
- SPY 20d return <= +1%
- beta-adjusted abnormal 20d return <= -10%
- signal close T -> entry T+1 open
- TP 0.75x ATR14, SL -5%, time stop 7 sessions
- risk 1.5%, max 5 positions, 10 bps round-trip costs

This script does not optimise parameters. It makes missing-data handling strict:
a signal is eligible only when PIT fundamentals and both context variables exist.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

import backtest_context_filters_pit_oos as base
import backtest as bt
import backtest_experiments as exp
import validate_sidi_candidate as val
import historical_membership as membership
import calibrate_pit_proxy_ttm as cal_ttm
import pit_proxy_ttm_fast as ttm_fast
import yfinance_pit_proxy as pit_base
import analyze_signal_context_features as feat
import backtest_context_filters as ctxmod

START = "2023-01-01"
END = "2026-09-10"
PRICE_START = "2022-01-01"


def main():
    print("="*110)
    print("SIDI COMPLETE-CASE REFERENCE — CURRENT AVAILABLE UNIVERSE")
    print("="*110)
    print("No delisted reconstruction. Missing required data => signal excluded. No optimisation.\n")

    master, sectors = feat.load_master()
    tickers = bt.load_tickers()
    fund_scores = bt.load_fundamental_scores()
    added = membership.load_date_added()

    prices = bt.download_prices(tickers, start_date=PRICE_START, end_date=END)
    # strict price availability: only tickers with usable downloaded history
    tickers = [t for t in tickers if t in prices and prices[t] is not None and not prices[t].empty]
    prices = {t: prices[t] for t in tickers}
    indicators = bt.build_indicators(prices)
    prices_idx = {t: feat.to_indexed(d) for t,d in prices.items()}
    pmap = base.price_maps(prices)

    spec = exp.signal_variant(12.0, False)
    spec["min_fund_score"] = 0.0
    raw, nraw = bt.build_signal_map(
        indicators, spec, spy_dict={}, date_from=START, date_to=END,
        fund_scores=fund_scores, sector_regime=None,
    )
    tech_map, member_stats = membership.filter_signal_map(raw, added)
    signal_dates = sorted({d for sigs in tech_map.values() for d in sigs})

    statements, failed = cal_ttm.fetch_full_statements(tickers)
    state_cache = ttm_fast.build_state_cache(statements, START, END)
    scores, coverage = base.dynamic_scores(signal_dates, tickers, sectors, state_cache, pmap, added)
    pit_map, pit_stats = base.filter_pit(tech_map, scores)

    feat.DOWNLOAD_START = PRICE_START
    refs = {s: feat.download_reference(s) for s in sorted(set(feat.SECTOR_ETF.values()) | {"SPY", "^VIX"})}
    market_breadth, sector_breadth = feat.breadth_maps(prices_idx, sectors, added)
    context = ctxmod.build_context(pit_map, prices_idx, sectors, refs, market_breadth, sector_breadth)

    def complete_candidate(r):
        return (
            base.present(r.get("spy20")) and
            base.present(r.get("abnormal20")) and
            r["spy20"] <= 1.0 and
            r["abnormal20"] <= -10.0
        )

    candidate_map, candidate_signals = ctxmod.subset_signal_map(pit_map, context, complete_candidate)

    rows=[]
    windows=[
        ("FULL_2023_2026", START, END),
        ("OOS_2023_TO_2024_08", "2023-01-01", "2024-08-10"),
        ("DEV_2024_08_TO_2026", "2024-08-11", END),
        ("RECENT_2025_09_TO_2026", "2025-09-01", END),
    ]
    for name, ws, we in windows:
        for label, smap in [("PIT_BASE", pit_map), ("PIT_CONTEXT_COMPLETE", candidate_map)]:
            st,_=val.run_sim(smap, prices, indicators, fund_scores, base.CANDIDATE, ws, we, cost_bps=base.COST_BPS)
            st.update({"window":name,"model":label})
            rows.append(st)
            print(f"{name:<24} {label:<22} trades={st['trades']:4d} WR={st['win_rate']:6.2f}% PF={st['profit_factor']:5.2f} Ret={st['total_return']:8.2f}% MDD={st['max_drawdown_mtm']:7.2f}% Days={st['avg_days']:4.2f}")

    summary=pd.DataFrame(rows).drop(columns=["config"],errors="ignore")
    summary.to_csv("sidi_complete_case_reference.csv",index=False)
    coverage.to_csv("sidi_complete_case_pit_coverage.csv",index=False)
    payload={
        "generated_at":datetime.utcnow().isoformat()+"Z",
        "policy":"Current available universe only; no delisted reconstruction; missing required signal-date data excludes the signal.",
        "period":[START,END],
        "downloaded_price_tickers":len(tickers),
        "technical_raw":nraw,
        "membership":member_stats,
        "pit_filter":pit_stats,
        "context_complete_signals":candidate_signals,
        "statement_failures":failed,
        "frozen":{"pit_min":6.66,"min_coverage":6,"spy20_max":1.0,"abnormal20_max":-10.0,"entry":"T+1 open","tp":"0.75xATR14","sl_pct":5.0,"time_stop":7,"risk_pct":1.5,"max_positions":5,"cost_bps_rt":10},
        "results":rows,
    }
    Path("sidi_complete_case_reference.json").write_text(json.dumps(payload,indent=2,ensure_ascii=False,default=str),encoding="utf-8")
    print("\nSaved complete-case reference outputs.")

if __name__ == "__main__":
    main()
