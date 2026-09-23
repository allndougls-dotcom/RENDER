"""Measure how much ATR0.75+T7 depends on the retrospective fund_score proxy.

This does not solve point-in-time fundamentals. It bounds the risk: if the
technical strategy remains profitable when the fundamental threshold is removed,
the current-score look-ahead cannot be the sole source of the observed edge.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import pandas as pd

import backtest as bt
import backtest_experiments as exp
import validate_sidi_candidate as val

START = "2024-08-11"
END = "2026-09-10"
COST_BPS = 10
THRESHOLDS = [0.0, 5.0, 6.5, 7.5]


def main() -> None:
    print("SIDI FUNDAMENTAL-DEPENDENCY VALIDATION")
    print(f"Frozen exits: TP=0.75xATR, SL=-5%, TS=7; cost={COST_BPS} bps RT")

    tickers = bt.load_tickers()
    fund_scores = bt.load_fundamental_scores()
    prices = bt.download_prices(tickers, start_date=START, end_date=END)
    indicators = bt.build_indicators(prices)

    rows = []
    for threshold in THRESHOLDS:
        spec = exp.signal_variant(12.0, False)
        spec["min_fund_score"] = threshold
        signal_map, n = bt.build_signal_map(
            indicators, spec, spy_dict={}, date_from=START, date_to=END,
            fund_scores=fund_scores, sector_regime=None,
        )
        experiment = exp.Experiment(
            name=f"FUND_{threshold:g}",
            target_mode="atr", atr_mult=0.75,
            stop_mode="fixed", stop_pct=0.05,
            time_stop=7,
            description=f"DD12 + min current-proxy fund_score {threshold:g}",
        )
        trades, equity, final_cap, ambiguous, same_day = val.run_sim(
            signal_map, prices, indicators, fund_scores, experiment,
            START, END, cost_bps=COST_BPS,
        )
        st = exp.stats_for(experiment, trades, equity, final_cap, n, ambiguous, same_day)
        rows.append(st)
        print(
            f"fund>={threshold:g}: signals={n:5d} trades={st['trades']:4d} "
            f"WR={st['win_rate']:6.2f}% PF={st['profit_factor']:5.2f} "
            f"Ret={st['total_return']:8.2f}% MDD={st['max_drawdown_mtm']:7.2f}% "
            f"Days={st['avg_days']:4.2f}"
        )

    out = pd.DataFrame(rows).drop(columns=["config"], errors="ignore")
    out.to_csv("sidi_fundamental_dependency.csv", index=False)
    print("Saved: sidi_fundamental_dependency.csv")


if __name__ == "__main__":
    main()
