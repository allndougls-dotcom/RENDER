"""Monte Carlo path-risk stress for the frozen SIDI complete-case candidate.

This is NOT a parameter optimisation.

It uses the same frozen candidate and realistic execution model as the previous
slot-randomization test (gap-aware stops + 10 bps round-trip costs), then takes
one pre-selected representative slot realization near the median of the prior
1,000-run slot stress.

Two complementary Monte Carlo methods are run:

1) PERMUTATION
   Same observed trades and same R multiples, only their order changes.
   Appropriate for path-risk questions (closed-equity MDD, losing streaks).
   Final compounded capital is order-invariant by construction.

2) MOVING-BLOCK BOOTSTRAP
   Resamples contiguous blocks of historical trade R multiples with replacement.
   This preserves some local dependence/regime clustering while allowing the
   outcome distribution itself to vary. It is used for distributions of final
   capital/return and drawdown.

Important: Monte Carlo drawdown is based on CLOSED-TRADE equity, not daily MTM
inside each trade. It therefore complements rather than replaces the backtest's
daily mark-to-market MDD.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import backtest_experiments as exp
import stress_execution_complete_case as exstress
import stress_slot_randomization as slot


N_SIMS = 10_000
BASE_SEED = 20260927
BLOCK_LEN = 5
RISK_LEVELS = [0.01, 0.015, 0.02]
COST_RATE = 10 / 10_000.0

# Representative slot realizations selected from the previous completed 1,000-run
# slot-randomization output as closest to the median PF/return/MDD combination.
WINDOWS = [
    ("FULL_2023_2026", "2023-01-01", "2026-09-10", 20261198),
    ("OOS_2023_TO_2024_08", "2023-01-01", "2024-08-10", 20360927),
    ("RECENT_2025_09_TO_2026", "2025-09-01", "2026-09-10", 20460932),
]


def capture_slot_trades(candidate_map, prices, indicators, fund_scores, start, end, seed):
    """Run the existing slot simulator and capture the exact closed trades it passes to stats_for."""
    captured = {}
    orig_stats = exp.stats_for

    def hooked_stats(exp_obj, trades, equity_curve, final_capital, signal_count, ambiguous_bars, same_day_exits):
        captured["trades"] = [dict(t) for t in trades]
        captured["equity"] = [dict(x) for x in equity_curve]
        captured["final_capital"] = float(final_capital)
        return orig_stats(
            exp_obj, trades, equity_curve, final_capital,
            signal_count, ambiguous_bars, same_day_exits,
        )

    exp.stats_for = hooked_stats
    try:
        stats = slot.simulate_randomized(
            candidate_map, prices, indicators, fund_scores,
            slot.REALISTIC, start, end, seed,
        )
    finally:
        exp.stats_for = orig_stats

    if "trades" not in captured:
        raise RuntimeError("Could not capture representative trades")
    return stats, captured["trades"], captured["equity"]


def net_r_multiple(trade: dict) -> float:
    """Recover net R including 10 bps RT cost from stored gross price return.

    price R = pnl_return / initial stop distance.
    implementation cost in R = cost_rate / stop_distance_fraction.
    """
    pnl_frac = float(trade["pnl_pct"]) / 100.0
    stop_frac = float(trade["stop_distance_pct"]) / 100.0
    if stop_frac <= 0:
        raise ValueError("Invalid stop distance")
    return pnl_frac / stop_frac - COST_RATE / stop_frac


def equity_metrics(r_seq: np.ndarray, risk_pct: float, initial=10_000.0):
    capital = float(initial)
    peak = capital
    max_dd = 0.0
    longest_loss_streak = 0
    cur_loss_streak = 0
    ruin = False

    for r in r_seq:
        factor = 1.0 + risk_pct * float(r)
        if factor <= 0:
            capital = 0.0
            ruin = True
            max_dd = 1.0
            break
        capital *= factor
        peak = max(peak, capital)
        dd = (peak - capital) / peak if peak > 0 else 1.0
        max_dd = max(max_dd, dd)
        if r <= 0:
            cur_loss_streak += 1
            longest_loss_streak = max(longest_loss_streak, cur_loss_streak)
        else:
            cur_loss_streak = 0

    return {
        "final_capital": capital,
        "total_return_pct": (capital / initial - 1.0) * 100.0,
        "max_drawdown_pct": -max_dd * 100.0,
        "max_loss_streak": int(longest_loss_streak),
        "ruin": bool(ruin),
    }


def moving_block_sample(r: np.ndarray, n: int, block_len: int, rng: np.random.Generator):
    if n == 0:
        return np.array([], dtype=float)
    if n <= block_len:
        return rng.choice(r, size=n, replace=True)
    starts = np.arange(0, n - block_len + 1)
    out = []
    while len(out) < n:
        s = int(rng.choice(starts))
        out.extend(r[s:s + block_len].tolist())
    return np.asarray(out[:n], dtype=float)


def summarize_numeric(values):
    s = pd.Series(values, dtype=float)
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


def run_method(r: np.ndarray, risk_pct: float, method: str, n_sims: int, seed: int):
    rng = np.random.default_rng(seed)
    rows = []
    n = len(r)
    for i in range(n_sims):
        if method == "permutation":
            seq = rng.permutation(r)
        elif method == "block_bootstrap":
            seq = moving_block_sample(r, n, BLOCK_LEN, rng)
        else:
            raise ValueError(method)
        m = equity_metrics(seq, risk_pct)
        m["sim"] = i
        rows.append(m)
    df = pd.DataFrame(rows)
    summary = {
        "n_sims": int(n_sims),
        "risk_pct": float(risk_pct * 100.0),
        "method": method,
        "final_capital": summarize_numeric(df.final_capital),
        "total_return_pct": summarize_numeric(df.total_return_pct),
        "max_drawdown_pct": summarize_numeric(df.max_drawdown_pct),
        "max_loss_streak": summarize_numeric(df.max_loss_streak),
        "probabilities_pct": {
            "finish_below_initial": float((df.final_capital < 10_000).mean() * 100),
            "mdd_ge_10": float((df.max_drawdown_pct <= -10).mean() * 100),
            "mdd_ge_15": float((df.max_drawdown_pct <= -15).mean() * 100),
            "mdd_ge_20": float((df.max_drawdown_pct <= -20).mean() * 100),
            "mdd_ge_25": float((df.max_drawdown_pct <= -25).mean() * 100),
            "loss_streak_ge_5": float((df.max_loss_streak >= 5).mean() * 100),
            "loss_streak_ge_7": float((df.max_loss_streak >= 7).mean() * 100),
            "loss_streak_ge_10": float((df.max_loss_streak >= 10).mean() * 100),
            "ruin": float(df.ruin.mean() * 100),
        },
    }
    return df, summary


def main():
    print("=" * 118)
    print("SIDI MONTE CARLO STRESS — REPRESENTATIVE SLOT PORTFOLIO, GAP-REAL 10 BPS")
    print("=" * 118)
    print(f"N={N_SIMS} per method/risk/window | block length={BLOCK_LEN} trades")
    print("Permutation = same trades, path risk only. Block bootstrap = empirical outcome uncertainty.\n")

    prices, indicators, fund_scores, candidate_map, coverage, meta = exstress.build_complete_case_inputs()

    all_runs = []
    summaries = {}
    representative = {}
    trade_rows = []

    for wi, (window, start, end, slot_seed) in enumerate(WINDOWS):
        print(f"\n{window}: {start} -> {end} | representative slot seed={slot_seed}")
        st, trades, _eq = capture_slot_trades(
            candidate_map, prices, indicators, fund_scores, start, end, slot_seed
        )
        r = np.asarray([net_r_multiple(t) for t in trades], dtype=float)
        representative[window] = {
            "slot_seed": slot_seed,
            "stats": st,
            "trades": len(trades),
            "r_mean": float(r.mean()) if len(r) else 0.0,
            "r_median": float(np.median(r)) if len(r) else 0.0,
            "r_min": float(r.min()) if len(r) else 0.0,
            "r_max": float(r.max()) if len(r) else 0.0,
            "observed_max_loss_streak": equity_metrics(r, 0.015)["max_loss_streak"],
        }
        print(
            f"  representative | trades={st['trades']} WR={st['win_rate']:.2f}% PF={st['profit_factor']:.3f} "
            f"Ret={st['total_return']:.2f}% MTM_MDD={st['max_drawdown_mtm']:.2f}% | "
            f"R mean={r.mean():.3f} min={r.min():.3f} max={r.max():.3f}"
        )

        for t, rv in zip(trades, r):
            trade_rows.append({"window": window, "slot_seed": slot_seed, **t, "net_r": rv})

        summaries[window] = {}
        for ri, risk_pct in enumerate(RISK_LEVELS):
            summaries[window][f"risk_{risk_pct*100:g}"] = {}
            for mi, method in enumerate(["permutation", "block_bootstrap"]):
                seed = BASE_SEED + wi * 1_000_000 + ri * 100_000 + mi * 10_000
                df, sm = run_method(r, risk_pct, method, N_SIMS, seed)
                df["window"] = window
                df["risk_pct"] = risk_pct * 100.0
                df["method"] = method
                all_runs.append(df)
                summaries[window][f"risk_{risk_pct*100:g}"][method] = sm
                p = sm["probabilities_pct"]
                print(
                    f"  risk={risk_pct*100:>3.1f}% {method:<15} | "
                    f"MDD med/p90tail/p95tail={sm['max_drawdown_pct']['median']:.2f}%/"
                    f"{sm['max_drawdown_pct']['p10']:.2f}%/{sm['max_drawdown_pct']['p05']:.2f}% | "
                    f"Ret med={sm['total_return_pct']['median']:.2f}% | "
                    f"P(MDD>=15)={p['mdd_ge_15']:.1f}% P(MDD>=20)={p['mdd_ge_20']:.1f}% "
                    f"P(end<initial)={p['finish_below_initial']:.1f}%"
                )

    runs = pd.concat(all_runs, ignore_index=True)
    runs.to_csv("sidi_monte_carlo_runs.csv", index=False)
    pd.DataFrame(trade_rows).to_csv("sidi_monte_carlo_representative_trades.csv", index=False)
    coverage.to_csv("sidi_monte_carlo_pit_coverage.csv", index=False)

    compact = []
    for window, risks in summaries.items():
        for risk_key, methods in risks.items():
            for method, sm in methods.items():
                compact.append({
                    "window": window,
                    "risk_pct": sm["risk_pct"],
                    "method": method,
                    "mdd_p05": sm["max_drawdown_pct"]["p05"],
                    "mdd_p10": sm["max_drawdown_pct"]["p10"],
                    "mdd_median": sm["max_drawdown_pct"]["median"],
                    "return_p10": sm["total_return_pct"]["p10"],
                    "return_median": sm["total_return_pct"]["median"],
                    "return_p90": sm["total_return_pct"]["p90"],
                    "capital_p10": sm["final_capital"]["p10"],
                    "capital_median": sm["final_capital"]["median"],
                    "capital_p90": sm["final_capital"]["p90"],
                    "loss_streak_median": sm["max_loss_streak"]["median"],
                    "loss_streak_p95": sm["max_loss_streak"]["p95"],
                    **sm["probabilities_pct"],
                })
    pd.DataFrame(compact).to_csv("sidi_monte_carlo_summary.csv", index=False)

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "methodology": {
            "n_sims": N_SIMS,
            "block_length_trades": BLOCK_LEN,
            "risk_levels_pct": [x * 100 for x in RISK_LEVELS],
            "execution": "gap-aware stop + 10 bps round-trip costs",
            "portfolio_sample": "fixed representative slot seed chosen near median of prior 1000-run slot stress",
            "permutation": "same R sample; order shuffled only; final compounded capital is order-invariant",
            "bootstrap": "moving-block resampling with replacement; preserves short local dependence",
            "drawdown": "closed-trade equity only; not daily mark-to-market",
        },
        "data_meta": meta,
        "representative": representative,
        "summaries": summaries,
    }
    Path("sidi_monte_carlo.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print("\nSaved Monte Carlo outputs.")


if __name__ == "__main__":
    main()
