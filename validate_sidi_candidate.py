"""Validation pass for the current SIDI candidate.

Candidate frozen BEFORE this validation:
    signal: DD60 >= 12%, RSI < 40, MACD histogram improving,
            declining volume, current proxy fund_score >= 6.5
    entry: next trading day OPEN
    target: 0.75 x ATR(14)
    stop: fixed -5%
    time stop: 7 trading sessions
    risk: 1.5% of realised capital
    max open positions: 5

This script does NOT optimise parameters. It validates the frozen candidate by:
1) round-trip trading-cost sensitivity applied to position notional;
2) independent temporal windows with capital reset;
3) comparison against the experimental 1.5xATR / 15-session base.

Important remaining biases:
- fund_score is the current score applied retrospectively, not point-in-time;
- universe is the current master list, so older windows can have survivorship bias;
- daily OHLC cannot resolve intraday order when both TP and SL touch; SIDI assumes STOP.
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

import pandas as pd

import backtest as bt
import backtest_experiments as exp


FULL_START = "2024-08-11"
FULL_END = "2026-09-10"

CANDIDATE = exp.Experiment(
    "ATR075_T7_VALIDATED",
    target_mode="atr",
    atr_mult=0.75,
    stop_mode="fixed",
    stop_pct=0.05,
    time_stop=7,
    description="Frozen candidate: TP 0.75xATR, SL 5%, time-stop 7 sessions",
)

BASELINE = exp.Experiment(
    "BASE_ATR15_T15",
    target_mode="atr",
    atr_mult=1.5,
    stop_mode="fixed",
    stop_pct=0.05,
    time_stop=15,
    description="Experimental baseline: TP 1.5xATR, SL 5%, time-stop 15 sessions",
)

# Round-trip implementation cost as a percentage of notional.
# 5 bps = 0.05%, 50 bps = 0.50%.
COST_BPS = [0, 5, 10, 20, 30, 50]

# These are validation slices, not optimisation/training windows.
# Parameters remain frozen in every slice and capital is reset to EUR 10k.
WINDOWS = [
    ("W1_2024H2", "2024-08-11", "2025-02-28"),
    ("W2_2025H1", "2025-03-01", "2025-08-31"),
    ("W3_2025H2", "2025-09-01", "2026-02-28"),
    ("W4_2026",   "2026-03-01", "2026-09-10"),
]

# Cache structures that are invariant across cost/window scenarios.
_ORIG_PRICE_INDEXES = exp.price_indexes
_ORIG_SCHEDULE_ENTRIES = exp.schedule_entries
_PRICE_CACHE = {}
_SCHEDULE_CACHE = {}


def cached_price_indexes(prices):
    key = id(prices)
    if key not in _PRICE_CACHE:
        _PRICE_CACHE[key] = _ORIG_PRICE_INDEXES(prices)
    return _PRICE_CACHE[key]


def cached_schedule_entries(signal_map, indicators):
    key = (id(signal_map), id(indicators))
    if key not in _SCHEDULE_CACHE:
        _SCHEDULE_CACHE[key] = _ORIG_SCHEDULE_ENTRIES(signal_map, indicators)
    return _SCHEDULE_CACHE[key]


exp.price_indexes = cached_price_indexes
exp.schedule_entries = cached_schedule_entries


def run_sim(signal_map, prices, indicators, fund_scores, experiment, start, end, cost_bps=0):
    """Run exp.simulate while charging round-trip costs on actual position notional."""
    old_start, old_end = exp.START_DATE, exp.END_DATE
    old_pnl = exp.pnl_from_exit

    cost_rate = float(cost_bps) / 10_000.0

    def pnl_with_cost(pos: dict, exit_price: float):
        pnl_pct, gross_pnl_eur = old_pnl(pos, exit_price)
        # risk_eur = notional * stop_distance_pct  ->  notional = risk / stop_distance.
        stop_dist = float(pos.get("stop_distance_pct", 0.0))
        if stop_dist <= 0:
            return pnl_pct, gross_pnl_eur
        notional_eur = float(pos["risk_eur"]) / stop_dist
        implementation_cost_eur = notional_eur * cost_rate
        return pnl_pct, gross_pnl_eur - implementation_cost_eur

    try:
        exp.START_DATE = start
        exp.END_DATE = end
        exp.pnl_from_exit = pnl_with_cost
        trades, equity, final_cap, ambiguous, same_day = exp.simulate(
            signal_map, prices, indicators, fund_scores, experiment
        )
        stats = exp.stats_for(
            experiment, trades, equity, final_cap,
            signal_count=sum(len(v) for v in signal_map.values()),
            ambiguous_bars=ambiguous,
            same_day_exits=same_day,
        )
    finally:
        exp.START_DATE, exp.END_DATE = old_start, old_end
        exp.pnl_from_exit = old_pnl

    stats["validation_start"] = start
    stats["validation_end"] = end
    stats["round_trip_cost_bps"] = cost_bps
    return stats, trades


def main():
    print("=" * 100)
    print("SIDI CANDIDATE VALIDATION — COST SENSITIVITY + TEMPORAL HOLDOUTS")
    print("=" * 100)
    print("Frozen candidate: DD12 / score>=6.5 / TP=0.75xATR / SL=5% / TS=7 / risk=1.5%")
    print("Costs are charged on position notional, round trip.\n")

    tickers = bt.load_tickers()
    fund_scores = bt.load_fundamental_scores()
    prices = bt.download_prices(tickers, start_date=FULL_START, end_date=FULL_END)
    indicators = bt.build_indicators(prices)

    signal_spec = exp.signal_variant(12.0, False)
    signal_map, nsignals = bt.build_signal_map(
        indicators, signal_spec, spy_dict={},
        date_from=FULL_START, date_to=FULL_END,
        fund_scores=fund_scores, sector_regime=None,
    )
    print(f"Signals in full period: {nsignals}\n")

    cost_rows = []
    print("COST SENSITIVITY — FULL PERIOD")
    print("-" * 100)
    for bps in COST_BPS:
        st, _ = run_sim(
            signal_map, prices, indicators, fund_scores,
            CANDIDATE, FULL_START, FULL_END, cost_bps=bps,
        )
        cost_rows.append(st)
        print(
            f"{bps:>2} bps RT | WR={st['win_rate']:>6.2f}% PF={st['profit_factor']:>5.2f} "
            f"Ret={st['total_return']:>8.2f}% CAGR={st['cagr']:>6.2f}% "
            f"MDD={st['max_drawdown_mtm']:>7.2f}% Trades={st['trades']:>4} AvgGross={st['avg_trade_pct']:>6.3f}%"
        )

    print("\nTEMPORAL HOLDOUTS — ZERO COST AND 10 BPS ROUND TRIP")
    print("-" * 100)
    window_rows = []
    for name, start, end in WINDOWS:
        for model in (BASELINE, CANDIDATE):
            for bps in (0, 10):
                st, _ = run_sim(
                    signal_map, prices, indicators, fund_scores,
                    model, start, end, cost_bps=bps,
                )
                st["window"] = name
                st["model"] = model.name
                window_rows.append(st)
                print(
                    f"{name:<10} {model.name:<20} {bps:>2}bps | "
                    f"WR={st['win_rate']:>6.2f}% PF={st['profit_factor']:>5.2f} "
                    f"Ret={st['total_return']:>7.2f}% MDD={st['max_drawdown_mtm']:>7.2f}% "
                    f"Trades={st['trades']:>3} Days={st['avg_days']:>4.2f}"
                )

    cost_df = pd.DataFrame(cost_rows)
    window_df = pd.DataFrame(window_rows)
    cost_df.to_csv("sidi_candidate_cost_sensitivity.csv", index=False)
    window_df.to_csv("sidi_candidate_temporal_validation.csv", index=False)

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "candidate": CANDIDATE.__dict__,
        "baseline": BASELINE.__dict__,
        "full_period": [FULL_START, FULL_END],
        "cost_bps_round_trip": COST_BPS,
        "windows": WINDOWS,
        "limitations": [
            "fund_score is current-score proxy, not point-in-time",
            "current-universe survivorship bias remains",
            "daily OHLC ambiguity resolves both-hit candles as STOP",
        ],
        "cost_sensitivity": cost_rows,
        "temporal_validation": window_rows,
    }
    Path("sidi_candidate_validation.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("\nSaved: sidi_candidate_cost_sensitivity.csv, sidi_candidate_temporal_validation.csv, sidi_candidate_validation.json")


if __name__ == "__main__":
    main()
