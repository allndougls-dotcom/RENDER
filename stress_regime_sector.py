"""Regime + sector robustness stress for frozen SIDI validation snapshot.

Consumes SIDI_VALIDATION_SNAPSHOT_V1 only. No data is downloaded and no
strategy threshold is re-optimised.

Two questions:
1) Does the frozen SIDI edge survive across simple ex-ante market regimes?
2) Does portfolio performance depend on any single GICS sector?

Slot allocation is randomized reproducibly when more FULL setups arrive than
free portfolio slots. Price indexes and trading dates are cached once so the
stress test does not rebuild ~500 ticker histories on every Monte Carlo pass.
"""
from __future__ import annotations

import gzip
import json
import pickle
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import backtest_experiments as exp
import backtest_context_filters_pit_oos as pit_base
import stress_parameter_perturbation as pert
import stress_slot_randomization as slot

SNAPSHOT_PATH = Path("sidi_validation_snapshot_v1.pkl.gz")
RESULTS_CSV = Path("sidi_regime_sector_runs.csv")
SUMMARY_CSV = Path("sidi_regime_sector_summary.csv")
DETAIL_CSV = Path("sidi_regime_sector_diagnostics.csv")
RESULTS_JSON = Path("sidi_regime_sector.json")

N_SIMS = 250
BASE_SEED = 20260928
WINDOWS = [
    ("FULL_2023_2026", "2023-01-01", "2026-09-10"),
    ("OOS_2023_TO_2024_08", "2023-01-01", "2024-08-10"),
    ("RECENT_2025_09_TO_2026", "2025-09-01", "2026-09-10"),
]


def load_snapshot():
    if not SNAPSHOT_PATH.exists():
        raise FileNotFoundError(SNAPSHOT_PATH)
    with gzip.open(SNAPSHOT_PATH, "rb") as f:
        snap = pickle.load(f)
    if snap.get("version") != "SIDI_VALIDATION_SNAPSHOT_V1":
        raise RuntimeError(f"Unexpected snapshot version: {snap.get('version')}")
    return snap


def subset_signal_map(signal_map, predicate):
    out = {}
    kept = 0
    for ticker, sigs in signal_map.items():
        for d, sig in sigs.items():
            if predicate(ticker, d, sig):
                out.setdefault(ticker, {})[d] = sig
                kept += 1
    return out, kept


def qstats(s):
    x = pd.to_numeric(pd.Series(s), errors="coerce").dropna()
    if x.empty:
        return {k: np.nan for k in ["min", "p10", "median", "p90", "max"]}
    return {
        "min": float(x.min()),
        "p10": float(x.quantile(.10)),
        "median": float(x.median()),
        "p90": float(x.quantile(.90)),
        "max": float(x.max()),
    }


def simulate_fast(signal_map, scheduled, rows_by_date, master_dates, fund_scores, start, end, seed):
    """Same execution logic as slot.simulate_randomized, with expensive indexes cached."""
    rng = np.random.default_rng(seed)
    all_dates = [d for d in master_dates if start <= d <= end]
    scenario = slot.REALISTIC

    capital = exp.INITIAL_CAPITAL
    open_pos = []
    trades = []
    equity_curve = []
    ambiguous_bars = 0
    same_day_exits = 0
    gap_events = 0
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
        nonlocal gap_events
        open_px = float(row["Open"])
        high = float(row["High"])
        low = float(row["Low"])
        close = float(row["Close"])

        if scenario.gap_aware_stop and existing_position and open_px <= pos["stop"]:
            gap_events += 1
            return "STOP_GAP", open_px * (1.0 - slip)

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
            entry = float(row["Open"]) * (1.0 + slip)
            atr = float(sig.get("atr_exact", 0.0))
            target, stop, tp_pct, stop_dist_pct = exp.target_and_stop(entry, atr, pit_base.CANDIDATE)
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
                "time_stop": pit_base.CANDIDATE.time_stop,
            }
            reason, exit_price = evaluate(
                pos, row,
                allow_time_stop=(pit_base.CANDIDATE.time_stop <= 1),
                existing_position=False,
            )
            if reason:
                close_position(pos, date, reason, float(exit_price))
            else:
                open_pos.append(pos)

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

    signal_count = sum(1 for sigs in signal_map.values() for d in sigs if start <= d <= end)
    st = exp.stats_for(
        pit_base.CANDIDATE, trades, equity_curve, capital,
        signal_count=signal_count,
        ambiguous_bars=ambiguous_bars,
        same_day_exits=same_day_exits,
    )
    st.update({
        "seed": int(seed),
        "oversubscribed_days": int(oversubscribed_days),
        "skipped_due_slots": int(skipped_due_slots),
        "eligible_entry_candidates": int(eligible_entries),
        "gap_stop_count": int(gap_events),
    })
    return st


def run_scenario(snapshot, rows_by_date, master_dates, scenario_group, scenario_name, signal_map, signals_kept, rows):
    scheduled = exp.schedule_entries(signal_map, snapshot["indicators"])
    for wi, (window, start, end) in enumerate(WINDOWS):
        for i in range(N_SIMS):
            seed = BASE_SEED + wi * 100_000 + i
            st = simulate_fast(
                signal_map, scheduled, rows_by_date, master_dates,
                snapshot["current_fund_scores"], start, end, seed,
            )
            rows.append({
                "scenario_group": scenario_group,
                "scenario": scenario_name,
                "window": window,
                "seed": seed,
                "signals_kept_all_period": signals_kept,
                **{k: v for k, v in st.items() if k != "config"},
            })


def summarize(df):
    rows = []
    for (group, scenario, window), g in df.groupby(["scenario_group", "scenario", "window"]):
        pf = qstats(g["profit_factor"])
        ret = qstats(g["total_return"])
        mdd = qstats(g["max_drawdown_mtm"])
        tr = qstats(g["trades"])
        rows.append({
            "scenario_group": group,
            "scenario": scenario,
            "window": window,
            "simulations": len(g),
            "signals_kept_all_period": int(g["signals_kept_all_period"].iloc[0]),
            "trades_median": tr["median"],
            "pf_p10": pf["p10"],
            "pf_median": pf["median"],
            "pf_p90": pf["p90"],
            "return_p10": ret["p10"],
            "return_median": ret["median"],
            "return_p90": ret["p90"],
            "mdd_p10": mdd["p10"],
            "mdd_median": mdd["median"],
            "mdd_p90": mdd["p90"],
            "prob_return_positive_pct": float((g["total_return"] > 0).mean() * 100),
            "prob_pf_gt1_pct": float((g["profit_factor"] > 1).mean() * 100),
            "prob_pf_ge1_2_pct": float((g["profit_factor"] >= 1.2).mean() * 100),
        })
    return pd.DataFrame(rows)


def main():
    print("=" * 132)
    print("SIDI REGIME + SECTOR STRESS — FROZEN SNAPSHOT / RANDOMIZED SLOTS")
    print("=" * 132)
    print(f"{N_SIMS} randomized-slot simulations per scenario/window; no parameter optimisation.\n")

    snapshot = load_snapshot()
    base_map, base_kept, missing = pert.build_signal_map(snapshot, pert.BASE)
    rows_by_date = exp.price_indexes(snapshot["prices"])
    master_dates = sorted({d for rows in rows_by_date.values() for d in rows})
    print(
        f"Snapshot={snapshot['version']} created={snapshot.get('created_at_utc')} | "
        f"FULL baseline signals={base_kept} missing_context={missing} | cached dates={len(master_dates)}"
    )

    run_rows = []
    diag_rows = []
    run_scenario(snapshot, rows_by_date, master_dates, "BASELINE", "ALL_FULL", base_map, base_kept, run_rows)

    def ctx(t, d):
        return snapshot["context"].get((t, d), {})

    regime_specs = [
        ("SPY20", "SPY20_LE_0", lambda t, d, s: pd.notna(ctx(t,d).get("spy20")) and float(ctx(t,d)["spy20"]) <= 0.0),
        ("SPY20", "SPY20_0_TO_1", lambda t, d, s: pd.notna(ctx(t,d).get("spy20")) and 0.0 < float(ctx(t,d)["spy20"]) <= 1.0),
        ("VIX", "VIX_PCT_LT_50", lambda t, d, s: pd.notna(ctx(t,d).get("vix_pct")) and float(ctx(t,d)["vix_pct"]) < 50.0),
        ("VIX", "VIX_PCT_GE_50", lambda t, d, s: pd.notna(ctx(t,d).get("vix_pct")) and float(ctx(t,d)["vix_pct"]) >= 50.0),
        ("SECTOR_BREADTH", "BREADTH_LT_50", lambda t, d, s: pd.notna(ctx(t,d).get("sector_breadth")) and float(ctx(t,d)["sector_breadth"]) < 50.0),
        ("SECTOR_BREADTH", "BREADTH_GE_50", lambda t, d, s: pd.notna(ctx(t,d).get("sector_breadth")) and float(ctx(t,d)["sector_breadth"]) >= 50.0),
    ]
    for group, name, pred in regime_specs:
        smap, n = subset_signal_map(base_map, pred)
        diag_rows.append({"type": "REGIME", "group": group, "scenario": name, "signals": n})
        run_scenario(snapshot, rows_by_date, master_dates, f"REGIME_{group}", name, smap, n, run_rows)

    for year in [2023, 2024, 2025, 2026]:
        smap, n = subset_signal_map(base_map, lambda t, d, s, y=year: str(d).startswith(str(y)))
        diag_rows.append({"type": "YEAR", "group": "YEAR", "scenario": str(year), "signals": n})
        run_scenario(snapshot, rows_by_date, master_dates, "REGIME_YEAR", f"YEAR_{year}", smap, n, run_rows)

    sectors_present = sorted({
        str(snapshot["sectors"].get(t, "Unknown"))
        for t in base_map
        if str(snapshot["sectors"].get(t, "Unknown")) != "Unknown"
    })
    for sector in sectors_present:
        smap, n = subset_signal_map(
            base_map,
            lambda t, d, s, sec=sector: str(snapshot["sectors"].get(t, "Unknown")) != sec,
        )
        diag_rows.append({
            "type": "LEAVE_ONE_SECTOR_OUT", "group": "SECTOR", "scenario": sector,
            "signals": n, "signals_removed": base_kept - n,
        })
        run_scenario(snapshot, rows_by_date, master_dates, "LEAVE_ONE_SECTOR_OUT", f"EX_{sector}", smap, n, run_rows)

    rdf = pd.DataFrame(run_rows)
    rdf.to_csv(RESULTS_CSV, index=False)
    sdf = summarize(rdf)
    sdf.to_csv(SUMMARY_CSV, index=False)
    pd.DataFrame(diag_rows).to_csv(DETAIL_CSV, index=False)

    print("\nBASELINE / REGIMES — FULL WINDOW")
    print("-" * 132)
    show = sdf[(sdf.window == "FULL_2023_2026") & (sdf.scenario_group != "LEAVE_ONE_SECTOR_OUT")]
    for r in show.itertuples(index=False):
        print(
            f"{r.scenario_group:<24} {r.scenario:<22} trades~{r.trades_median:5.0f} "
            f"PF p10/med/p90={r.pf_p10:.2f}/{r.pf_median:.2f}/{r.pf_p90:.2f} "
            f"Ret med={r.return_median:7.2f}% MDD med={r.mdd_median:7.2f}%"
        )

    print("\nLEAVE-ONE-SECTOR-OUT — FULL WINDOW")
    print("-" * 132)
    sec = sdf[(sdf.window == "FULL_2023_2026") & (sdf.scenario_group == "LEAVE_ONE_SECTOR_OUT")]
    for r in sec.itertuples(index=False):
        print(
            f"{r.scenario:<38} trades~{r.trades_median:5.0f} "
            f"PF p10/med/p90={r.pf_p10:.2f}/{r.pf_median:.2f}/{r.pf_p90:.2f} "
            f"Ret med={r.return_median:7.2f}% MDD med={r.mdd_median:7.2f}% "
            f"P(PF>=1.2)={r.prob_pf_ge1_2_pct:5.1f}%"
        )

    RESULTS_JSON.write_text(json.dumps({
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "snapshot_version": snapshot["version"],
        "snapshot_created_at_utc": snapshot.get("created_at_utc"),
        "method": "regime diagnostics + leave-one-sector-out with randomized 5-slot allocation",
        "n_sims_per_scenario_window": N_SIMS,
        "base_seed": BASE_SEED,
        "frozen_strategy": pert.BASE,
        "execution": "T+1 open; gap-aware SL; stop-first ambiguity; 10bps RT; risk 1.5%; max 5",
        "regime_definitions": {
            "SPY20": ["<=0%", "0%..+1%"],
            "VIX_percentile_252": ["<50", ">=50"],
            "sector_breadth_above_SMA50": ["<50%", ">=50%"],
            "calendar_year": [2023, 2024, 2025, 2026],
        },
        "interpretation": "Descriptive robustness only. Do not promote any regime or sector result into a new filter from this test.",
        "summary": sdf.to_dict("records"),
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print("\nSaved regime + sector stress outputs.")


if __name__ == "__main__":
    main()
