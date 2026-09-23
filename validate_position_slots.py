"""Sensitivity of the frozen SIDI candidate to max simultaneous positions.

The experimental backtest used 5 slots, while the current Control Center uses 3.
This validation keeps every signal/exit/risk/cost parameter frozen and changes
ONLY MAX_POSITIONS so production can be aligned with the tested assumption.
"""
from __future__ import annotations

import pandas as pd

import backtest as bt
import backtest_experiments as exp
import validate_sidi_candidate as val

START = "2024-08-11"
END = "2026-09-10"
COST_BPS = 10
SLOTS = [2, 3, 4, 5]

CANDIDATE = exp.Experiment(
    "ATR075_T7",
    target_mode="atr",
    atr_mult=0.75,
    stop_mode="fixed",
    stop_pct=0.05,
    time_stop=7,
    description="Frozen candidate; only max positions changes",
)


def main():
    print("SIDI SLOT SENSITIVITY — candidate frozen, 10 bps RT")
    tickers = bt.load_tickers()
    fund_scores = bt.load_fundamental_scores()
    prices = bt.download_prices(tickers, start_date=START, end_date=END)
    indicators = bt.build_indicators(prices)

    spec = exp.signal_variant(12.0, False)
    spec["min_fund_score"] = 6.5
    signal_map, n = bt.build_signal_map(
        indicators, spec, spy_dict={}, date_from=START, date_to=END,
        fund_scores=fund_scores, sector_regime=None,
    )
    print(f"Signals={n}")

    old_slots = exp.MAX_POSITIONS
    rows = []
    try:
        for slots in SLOTS:
            exp.MAX_POSITIONS = slots
            st, trades = val.run_sim(
                signal_map, prices, indicators, fund_scores, CANDIDATE,
                START, END, cost_bps=COST_BPS,
            )
            st["max_positions"] = slots
            rows.append(st)
            print(
                f"slots={slots} trades={st['trades']:4d} WR={st['win_rate']:6.2f}% "
                f"PF={st['profit_factor']:5.2f} Ret={st['total_return']:8.2f}% "
                f"CAGR={st['cagr']:6.2f}% MDD={st['max_drawdown_mtm']:7.2f}% "
                f"Days={st['avg_days']:4.2f}"
            )
    finally:
        exp.MAX_POSITIONS = old_slots

    pd.DataFrame(rows).drop(columns=["config"], errors="ignore").to_csv(
        "sidi_slot_sensitivity.csv", index=False
    )
    print("Saved sidi_slot_sensitivity.csv")


if __name__ == "__main__":
    main()
