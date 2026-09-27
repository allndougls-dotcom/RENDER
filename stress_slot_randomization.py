"""Slot-randomization stress for the frozen complete-case SIDI candidate.

Purpose
-------
Measure how much portfolio results depend on the arbitrary ordering of FULL
setups when more candidates arrive than the five available portfolio slots.

Everything else is frozen:
- complete-case PIT candidate
- DD60 >= 12%, technical setup, PIT proxy >= 6.66
- SPY20 <= +1%, abnormal20 <= -10%
- T+1 OPEN entry, TP 0.75xATR, SL -5%, T7
- 1.5% risk, max 5 positions
- GAP-AWARE stop execution + 10 bps round-trip costs
- conservative STOP-first daily OHLC ambiguity

No ranking is fitted. On oversubscribed entry days candidates are shuffled with
reproducible RNG seeds, then only the available number of slots are opened.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import backtest_experiments as exp
import stress_execution_complete_case as exstress
import backtest_context_filters_pit_oos as base


N_SIMS = 1000
BASE_SEED = 20260927
REALISTIC = exstress.ExecutionScenario(
    "GAP_REAL_10BPS", True, 10, 0,
    "Gap-aware stop + 10 bps round-trip costs",
)
WINDOWS = [
    ("FULL_2023_2026", "2023-01-01", "2026-09-10"),
    ("OOS_2023_TO_2024_08", "2023-01-01", "2024-08-10"),
    ("RECENT_2025_09_TO_2026", "2025-09-01", "2026-09-10"),
]


def simulate_randomized(signal_map, prices, indicators, fund_scores, scenario, start, end, seed):
    rng = np.random.default_rng(seed)
    rows_by_date = exp.price_indexes(prices)
    scheduled = exp.schedule_entries(signal_map, indicators)
    all_dates = sorted({d for rows in rows_by_date.values() for d in rows})
    all_dates = [d for d in all_dates if start <= d <= end]

    capital = exp.INITIAL_CAPITAL
    open_pos = []
    trades = []
    equity_curve = []
    ambiguous_bars = 0
    same_day_exits = 0
    gap_events = []
    oversubscribed_days = 0
    skipped_due_slots = 0
    eligible_entries = 0
    slip = float(scenario.slippage_bps) / 10_000.0
    cost_rate = float(scenario.cost_bps_rt) / 10_000.0

    def pnl_net(pos, exit_price):
        pnl_pct = (exit_price - pos["entry"]) / pos["entry"]
        gross = pos["risk_eur"] * (pnl_pct / pos["stop_distance_pct"])
        notional = pos["risk_eur"] / pos["stop_distance_pct"]
        cost = notional * cost_rate
        return pnl_pct, gross - cost

    def close_position(pos, date, reason, exit_price):
        nonlocal capital, ambiguous_bars, same_day_exits
        pnl_pct, pnl_eur = pnl_net(pos, exit_price)
        capital += pnl_eur
        if reason == "STOP_BOTH_TOUCHED":
            ambiguous_bars += 1
        if date == pos["entry_date"]:
            same_day_exits += 1
        trades.append({
            "ticker": pos["ticker"],
            "signal_date": pos["signal_date"],
            "entry_date": pos["entry_date"],
            "exit_date": date,
            "entry": round(pos["entry"], 4),
            "exit": round(exit_price, 4),
            "target": round(pos["target"], 4),
            "stop": round(pos["stop"], 4),
            "target_pct": round(pos["target_pct"] * 100, 4),
            "stop_distance_pct": round(pos["stop_distance_pct"] * 100, 4),
            "atr": round(pos["atr"], 4),
            "rsi": round(pos["rsi"], 2),
            "dd": round(pos["dd"], 2),
            "fund_score": round(pos["fund_score"], 2),
            "days": pos["days_held"],
            "exit_reason": reason,
            "pnl_pct": round(pnl_pct * 100, 4),
            "pnl_eur": round(pnl_eur, 2),
            "outcome": "WIN" if pnl_eur > 0 else "LOSS",
            "capital_after": round(capital, 2),
        })

    def evaluate(pos, row, allow_time_stop=True, existing_position=True):
        open_px = float(row["Open"])
        high = float(row["High"])
        low = float(row["Low"])
        close = float(row["Close"])

        if scenario.gap_aware_stop and existing_position and open_px <= pos["stop"]:
            fill = open_px * (1.0 - slip)
            gap_events.append({
                "ticker": pos["ticker"], "date": str(row["Date"])[:10],
                "stop": pos["stop"], "open": open_px, "fill": fill,
                "gap_beyond_stop_pct": (open_px / pos["stop"] - 1.0) * 100.0,
            })
            return "STOP_GAP", fill

        stop_hit = low <= pos["stop"]
        target_hit = high >= pos["target"]
        stop_fill = pos["stop"] * (1.0 - slip)
        if stop_hit and target_hit:
            return "STOP_BOTH_TOUCHED", stop_fill
        if stop_hit:
            return "STOP", stop_fill
        if target_hit:
            return "TARGET", pos["target"]
        if allow_time_stop and pos["days_held"] >= pos["time_stop"]:
            return "TIME_STOP", close * (1.0 - slip)
        return None, None

    for date in all_dates:
        # Existing positions are processed first, exactly as in the reference engine.
        survivors = []
        for pos in open_pos:
            row = rows_by_date.get(pos["ticker"], {}).get(date)
            if row is None:
                survivors.append(pos)
                continue
            pos["days_held"] += 1
            reason, exit_price = evaluate(pos, row, allow_time_stop=True, existing_position=True)
            if reason:
                close_position(pos, date, reason, float(exit_price))
            else:
                survivors.append(pos)
        open_pos = survivors

        open_tickers = {p["ticker"] for p in open_pos}
        candidates = [s for s in scheduled.get(date, []) if s["ticker"] not in open_tickers]
        slots = max(0, exp.MAX_POSITIONS - len(open_pos))
        eligible_entries += len(candidates)

        # Randomize ONLY when slots are scarce. Otherwise preserve the set exactly.
        if len(candidates) > slots:
            oversubscribed_days += 1
            skipped_due_slots += len(candidates) - slots
            order = rng.permutation(len(candidates))
            candidates = [candidates[i] for i in order]

        for sig in candidates[:slots]:
            ticker = sig["ticker"]
            row = rows_by_date.get(ticker, {}).get(date)
            if row is None or pd.isna(row.get("Open", np.nan)):
                continue

            raw_open = float(row["Open"])
            entry = raw_open * (1.0 + slip)
            atr = float(sig.get("atr_exact", 0.0))
            target, stop, tp_pct, stop_dist_pct = exp.target_and_stop(entry, atr, base.CANDIDATE)
            if stop_dist_pct <= 0:
                continue

            fscore = float(fund_scores.get(ticker, {}).get("fund_score", 0.0))
            pos = {
                "ticker": ticker,
                "signal_date": sig["signal_date"],
                "entry_date": date,
                "entry": entry,
                "target": target,
                "stop": stop,
                "target_pct": tp_pct,
                "stop_distance_pct": stop_dist_pct,
                "atr": atr,
                "risk_eur": capital * exp.RISK_PCT,
                "rsi": float(sig["rsi"]),
                "dd": float(sig["dd"]),
                "fund_score": fscore,
                "days_held": 1,
                "time_stop": base.CANDIDATE.time_stop,
            }

            reason, exit_price = evaluate(
                pos, row,
                allow_time_stop=(base.CANDIDATE.time_stop <= 1),
                existing_position=False,
            )
            if reason:
                close_position(pos, date, reason, float(exit_price))
            else:
                open_pos.append(pos)
                open_tickers.add(ticker)

        mtm_equity = capital
        for pos in open_pos:
            row = rows_by_date.get(pos["ticker"], {}).get(date)
            if row is None:
                continue
            close = float(row["Close"])
            pnl_pct = (close - pos["entry"]) / pos["entry"]
            mtm_equity += pos["risk_eur"] * (pnl_pct / pos["stop_distance_pct"])
        equity_curve.append({"date": date, "equity": round(mtm_equity, 2)})

    final_date = all_dates[-1] if all_dates else end
    for pos in list(open_pos):
        row = rows_by_date.get(pos["ticker"], {}).get(final_date)
        d = final_date
        if row is None:
            ticker_dates = [x for x in rows_by_date.get(pos["ticker"], {}) if x <= end]
            if not ticker_dates:
                continue
            d = max(ticker_dates)
            row = rows_by_date[pos["ticker"]][d]
        close_position(pos, d, "END_OF_PERIOD", float(row["Close"]) * (1.0 - slip))

    st = exp.stats_for(
        base.CANDIDATE, trades, equity_curve, capital,
        signal_count=sum(len(v) for v in signal_map.values()),
        ambiguous_bars=ambiguous_bars, same_day_exits=same_day_exits,
    )
    st.update({
        "seed": int(seed),
        "oversubscribed_days": int(oversubscribed_days),
        "skipped_due_slots": int(skipped_due_slots),
        "eligible_entry_candidates": int(eligible_entries),
        "gap_stop_count": int(len(gap_events)),
    })
    return st


def pct_row(s: pd.Series):
    return {
        "min": float(s.min()),
        "p05": float(s.quantile(0.05)),
        "p10": float(s.quantile(0.10)),
        "p25": float(s.quantile(0.25)),
        "median": float(s.median()),
        "p75": float(s.quantile(0.75)),
        "p90": float(s.quantile(0.90)),
        "p95": float(s.quantile(0.95)),
        "max": float(s.max()),
        "mean": float(s.mean()),
    }


def summarize(df: pd.DataFrame):
    out = {}
    for col in ["profit_factor", "total_return", "max_drawdown_mtm", "win_rate", "trades", "skipped_due_slots"]:
        out[col] = pct_row(pd.to_numeric(df[col], errors="coerce").dropna())
    out["probabilities_pct"] = {
        "return_gt_0": float((df.total_return > 0).mean() * 100),
        "pf_gt_1": float((df.profit_factor > 1).mean() * 100),
        "pf_ge_1_2": float((df.profit_factor >= 1.2).mean() * 100),
        "pf_ge_1_3": float((df.profit_factor >= 1.3).mean() * 100),
        "mdd_better_than_minus15": float((df.max_drawdown_mtm > -15).mean() * 100),
        "mdd_better_than_minus20": float((df.max_drawdown_mtm > -20).mean() * 100),
    }
    return out


def main():
    print("=" * 116)
    print("SIDI SLOT RANDOMIZATION STRESS — 5 SLOTS, GAP-REAL 10 BPS")
    print("=" * 116)
    print(f"Simulations per window: {N_SIMS} | base seed: {BASE_SEED}")
    print("No ranking optimisation. Random choice occurs only when candidates > free slots.\n")

    prices, indicators, fund_scores, candidate_map, coverage, meta = exstress.build_complete_case_inputs()

    rows = []
    summaries = {}
    deterministic = {}

    for wi, (name, start, end) in enumerate(WINDOWS):
        print(f"\n{name}: {start} -> {end}")
        det, _, gaps = exstress.run_scenario(
            candidate_map, prices, indicators, fund_scores,
            REALISTIC, start, end,
        )
        deterministic[name] = det
        print(
            f"  deterministic | trades={det['trades']:4d} WR={det['win_rate']:6.2f}% "
            f"PF={det['profit_factor']:5.2f} Ret={det['total_return']:8.2f}% "
            f"MDD={det['max_drawdown_mtm']:7.2f}% gaps={len(gaps)}"
        )

        window_rows = []
        for i in range(N_SIMS):
            seed = BASE_SEED + wi * 100_000 + i
            st = simulate_randomized(
                candidate_map, prices, indicators, fund_scores,
                REALISTIC, start, end, seed,
            )
            st["window"] = name
            window_rows.append(st)
            rows.append(st)
            if (i + 1) % 100 == 0:
                print(f"  simulations {i+1}/{N_SIMS}")

        wdf = pd.DataFrame(window_rows)
        summaries[name] = summarize(wdf)
        sm = summaries[name]
        print(
            f"  PF p10/med/p90={sm['profit_factor']['p10']:.2f}/"
            f"{sm['profit_factor']['median']:.2f}/{sm['profit_factor']['p90']:.2f} | "
            f"Ret p10/med/p90={sm['total_return']['p10']:.2f}%/"
            f"{sm['total_return']['median']:.2f}%/{sm['total_return']['p90']:.2f}% | "
            f"MDD p10/med/p90={sm['max_drawdown_mtm']['p10']:.2f}%/"
            f"{sm['max_drawdown_mtm']['median']:.2f}%/{sm['max_drawdown_mtm']['p90']:.2f}%"
        )
        print(
            f"  P(return>0)={sm['probabilities_pct']['return_gt_0']:.1f}% "
            f"P(PF>1)={sm['probabilities_pct']['pf_gt_1']:.1f}% "
            f"P(PF>=1.2)={sm['probabilities_pct']['pf_ge_1_2']:.1f}% "
            f"P(PF>=1.3)={sm['probabilities_pct']['pf_ge_1_3']:.1f}%"
        )

    rdf = pd.DataFrame(rows)
    rdf.to_csv("sidi_slot_randomization_runs.csv", index=False)
    coverage.to_csv("sidi_slot_randomization_pit_coverage.csv", index=False)

    # Compact percentile table for easy inspection.
    compact = []
    for name, sm in summaries.items():
        compact.append({
            "window": name,
            "pf_p10": sm["profit_factor"]["p10"],
            "pf_median": sm["profit_factor"]["median"],
            "pf_p90": sm["profit_factor"]["p90"],
            "ret_p10": sm["total_return"]["p10"],
            "ret_median": sm["total_return"]["median"],
            "ret_p90": sm["total_return"]["p90"],
            "mdd_p10": sm["max_drawdown_mtm"]["p10"],
            "mdd_median": sm["max_drawdown_mtm"]["median"],
            "mdd_p90": sm["max_drawdown_mtm"]["p90"],
            "prob_return_positive_pct": sm["probabilities_pct"]["return_gt_0"],
            "prob_pf_gt_1_pct": sm["probabilities_pct"]["pf_gt_1"],
            "prob_pf_ge_1_2_pct": sm["probabilities_pct"]["pf_ge_1_2"],
            "prob_pf_ge_1_3_pct": sm["probabilities_pct"]["pf_ge_1_3"],
            "skipped_slots_median": sm["skipped_due_slots"]["median"],
        })
    pd.DataFrame(compact).to_csv("sidi_slot_randomization_summary.csv", index=False)

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "n_simulations_per_window": N_SIMS,
        "base_seed": BASE_SEED,
        "execution": asdict(REALISTIC),
        "frozen": {
            "pit_min": 6.66, "min_coverage": 6,
            "spy20_max": 1.0, "abnormal20_max": -10.0,
            "entry": "T+1 open", "tp": "0.75xATR14", "sl_pct": 5.0,
            "time_stop": 7, "risk_pct": 1.5, "max_positions": 5,
        },
        "windows": WINDOWS,
        "deterministic_reference": deterministic,
        "randomized_summary": summaries,
        "input_meta": meta,
    }
    Path("sidi_slot_randomization.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print("\nSaved slot-randomization outputs.")


if __name__ == "__main__":
    main()
