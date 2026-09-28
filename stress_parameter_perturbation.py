"""One-at-a-time parameter perturbation for frozen SIDI validation snapshot.

This is a robustness test, NOT an optimisation. Exactly one parameter moves at
a time around the current frozen candidate; every other rule remains fixed.
The script consumes SIDI_VALIDATION_SNAPSHOT_V1 so every comparison uses the
same byte-identical market/fundamental/context inputs.

Execution baseline:
- T+1 open
- gap-aware stop execution
- conservative stop-first if TP+SL touched in same daily bar
- 10 bps round-trip implementation cost
- 1.5% risk / max 5 positions
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
import stress_execution_complete_case as exstress

SNAPSHOT_PATH = Path("sidi_validation_snapshot_v1.pkl.gz")
RESULTS_CSV = Path("sidi_parameter_perturbation.csv")
SUMMARY_CSV = Path("sidi_parameter_perturbation_family_summary.csv")
RESULTS_JSON = Path("sidi_parameter_perturbation.json")

BASE = {
    "dd_min": 12.0,
    "fund_min": 6.66,
    "spy20_max": 1.0,
    "abnormal20_max": -10.0,
    "atr_mult": 0.75,
    "sl_pct": 0.05,
    "time_stop": 7,
}

WINDOWS = [
    ("FULL_2023_2026", "2023-01-01", "2026-09-10"),
    ("OOS_2023_TO_2024_08", "2023-01-01", "2024-08-10"),
    ("RECENT_2025_09_TO_2026", "2025-09-01", "2026-09-10"),
]

SCENARIO = exstress.ExecutionScenario(
    "GAP_REAL_10BPS", True, 10, 0,
    "Gap-aware stop + 10 bps round-trip costs",
)

# The middle value is always the frozen baseline. No winner will be selected.
FAMILIES = {
    "DD60_MIN_PCT": [10.0, 12.0, 14.0],
    "FUND_PIT_MIN": [6.40, 6.66, 6.90],
    "TP_ATR_MULT": [0.60, 0.75, 0.90],
    "SL_PCT": [0.04, 0.05, 0.06],
    "TIME_STOP": [5, 7, 9],
    "SPY20_MAX_PCT": [0.0, 1.0, 2.0],
    "ABNORMAL20_MAX_PCT": [-8.0, -10.0, -12.0],
}


def present(x):
    return x is not None and pd.notna(x)


def build_signal_map(snapshot, cfg):
    out = {}
    kept = 0
    missing_context = 0
    for ticker, sigs in snapshot["signals"].items():
        for d, sig in sigs.items():
            dd = float(sig.get("dd", np.nan))
            fund = float(sig.get("pit_fund_score", np.nan))
            cov = int(sig.get("pit_coverage", 0))
            ctx = snapshot["context"].get((ticker, d), {})
            spy20 = ctx.get("spy20")
            abnormal20 = ctx.get("abnormal20")
            if cov < 6 or not np.isfinite(dd) or not np.isfinite(fund):
                continue
            if dd > -float(cfg["dd_min"]):
                continue
            if fund < float(cfg["fund_min"]):
                continue
            if not present(spy20) or not present(abnormal20):
                missing_context += 1
                continue
            if float(spy20) > float(cfg["spy20_max"]):
                continue
            if float(abnormal20) > float(cfg["abnormal20_max"]):
                continue
            item = dict(sig)
            item["shadow_spy20"] = float(spy20)
            item["shadow_abnormal20"] = float(abnormal20)
            out.setdefault(ticker, {})[d] = item
            kept += 1
    return out, kept, missing_context


def make_experiment(cfg, name):
    return exp.Experiment(
        name,
        target_mode="atr", atr_mult=float(cfg["atr_mult"]),
        stop_mode="fixed", stop_pct=float(cfg["sl_pct"]),
        time_stop=int(cfg["time_stop"]),
        description="One-at-a-time perturbation; gap-real 10bps",
    )


def run_sim(snapshot, signal_map, candidate, start, end):
    old_candidate = pit_base.CANDIDATE
    old_start, old_end = exp.START_DATE, exp.END_DATE
    try:
        # stress_execution_complete_case references the same imported pit_base module.
        pit_base.CANDIDATE = candidate
        exp.START_DATE, exp.END_DATE = start, end
        trades, equity, final_cap, ambiguous, same_day, gaps = exstress.simulate_execution(
            signal_map,
            snapshot["prices"],
            snapshot["indicators"],
            snapshot["current_fund_scores"],
            SCENARIO,
            start,
            end,
        )
        signal_count = sum(
            1 for sigs in signal_map.values() for d in sigs if start <= d <= end
        )
        st = exp.stats_for(
            candidate, trades, equity, final_cap,
            signal_count=signal_count,
            ambiguous_bars=ambiguous,
            same_day_exits=same_day,
        )
        st.update({
            "gap_stop_count": len(gaps),
            "validation_start": start,
            "validation_end": end,
            "round_trip_cost_bps": 10,
        })
        return st
    finally:
        pit_base.CANDIDATE = old_candidate
        exp.START_DATE, exp.END_DATE = old_start, old_end


def config_for(family, value):
    cfg = dict(BASE)
    if family == "DD60_MIN_PCT":
        cfg["dd_min"] = float(value)
    elif family == "FUND_PIT_MIN":
        cfg["fund_min"] = float(value)
    elif family == "TP_ATR_MULT":
        cfg["atr_mult"] = float(value)
    elif family == "SL_PCT":
        cfg["sl_pct"] = float(value)
    elif family == "TIME_STOP":
        cfg["time_stop"] = int(value)
    elif family == "SPY20_MAX_PCT":
        cfg["spy20_max"] = float(value)
    elif family == "ABNORMAL20_MAX_PCT":
        cfg["abnormal20_max"] = float(value)
    else:
        raise ValueError(f"Unknown family: {family}")
    return cfg


def is_baseline_value(family, value):
    baseline = {
        "DD60_MIN_PCT": 12.0,
        "FUND_PIT_MIN": 6.66,
        "TP_ATR_MULT": 0.75,
        "SL_PCT": 0.05,
        "TIME_STOP": 7,
        "SPY20_MAX_PCT": 1.0,
        "ABNORMAL20_MAX_PCT": -10.0,
    }[family]
    return abs(float(value) - float(baseline)) < 1e-12


def main():
    print("=" * 126)
    print("SIDI PARAMETER PERTURBATION — ONE VARIABLE AT A TIME / FROZEN SNAPSHOT")
    print("=" * 126)
    print("No optimisation. Robustness means neighbouring values should not destroy the edge.\n")

    if not SNAPSHOT_PATH.exists():
        raise FileNotFoundError(f"Missing frozen snapshot: {SNAPSHOT_PATH}")
    with gzip.open(SNAPSHOT_PATH, "rb") as f:
        snapshot = pickle.load(f)
    if snapshot.get("version") != "SIDI_VALIDATION_SNAPSHOT_V1":
        raise RuntimeError(f"Unexpected snapshot version: {snapshot.get('version')}")

    print(
        f"Snapshot={snapshot['version']} created={snapshot.get('created_at_utc')} "
        f"broad signals={snapshot['meta']['coverage_filter']['kept']}"
    )

    rows = []
    maps_cache = {}
    for family, values in FAMILIES.items():
        print(f"\n{family}")
        print("-" * 126)
        for value in values:
            cfg = config_for(family, value)
            map_key = (
                cfg["dd_min"], cfg["fund_min"], cfg["spy20_max"], cfg["abnormal20_max"]
            )
            if map_key not in maps_cache:
                maps_cache[map_key] = build_signal_map(snapshot, cfg)
            signal_map, signals_kept, missing_context = maps_cache[map_key]
            candidate = make_experiment(cfg, f"PERTURB_{family}_{value}")
            baseline_flag = is_baseline_value(family, value)

            for wname, start, end in WINDOWS:
                st = run_sim(snapshot, signal_map, candidate, start, end)
                row = {
                    "family": family,
                    "value": value,
                    "is_frozen_baseline": baseline_flag,
                    "window": wname,
                    "signals_kept_all_period": signals_kept,
                    "missing_context_seen": missing_context,
                    "dd_min": cfg["dd_min"],
                    "fund_min": cfg["fund_min"],
                    "spy20_max": cfg["spy20_max"],
                    "abnormal20_max": cfg["abnormal20_max"],
                    "atr_mult": cfg["atr_mult"],
                    "sl_pct": cfg["sl_pct"],
                    "time_stop": cfg["time_stop"],
                }
                row.update({k: v for k, v in st.items() if k != "config"})
                rows.append(row)
                print(
                    f"  {str(value):>6} | {wname:<22} sig={st['signal_count']:4d} "
                    f"trades={st['trades']:3d} WR={st['win_rate']:6.2f}% "
                    f"PF={st['profit_factor']:5.2f} Ret={st['total_return']:8.2f}% "
                    f"MDD={st['max_drawdown_mtm']:7.2f}%"
                )

    df = pd.DataFrame(rows)
    df.to_csv(RESULTS_CSV, index=False)

    # Family robustness summary: describe the neighbourhood, never rank/select a winner.
    summary_rows = []
    for (family, window), g in df.groupby(["family", "window"]):
        g = g.copy()
        base_row = g[g["is_frozen_baseline"]]
        summary_rows.append({
            "family": family,
            "window": window,
            "variants": len(g),
            "all_positive_return": bool((g["total_return"] > 0).all()),
            "all_pf_gt_1": bool((g["profit_factor"] > 1).all()),
            "all_pf_ge_1_2": bool((g["profit_factor"] >= 1.2).all()),
            "pf_min": float(g["profit_factor"].min()),
            "pf_median": float(g["profit_factor"].median()),
            "pf_max": float(g["profit_factor"].max()),
            "return_min": float(g["total_return"].min()),
            "return_median": float(g["total_return"].median()),
            "return_max": float(g["total_return"].max()),
            "mdd_worst": float(g["max_drawdown_mtm"].min()),
            "mdd_median": float(g["max_drawdown_mtm"].median()),
            "baseline_pf": float(base_row.iloc[0]["profit_factor"]) if len(base_row) else np.nan,
            "baseline_return": float(base_row.iloc[0]["total_return"]) if len(base_row) else np.nan,
            "baseline_mdd": float(base_row.iloc[0]["max_drawdown_mtm"]) if len(base_row) else np.nan,
        })
    sdf = pd.DataFrame(summary_rows)
    sdf.to_csv(SUMMARY_CSV, index=False)

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "snapshot_version": snapshot["version"],
        "snapshot_created_at_utc": snapshot.get("created_at_utc"),
        "method": "one_parameter_at_a_time",
        "execution": "T+1 open; gap-aware SL; stop-first OHLC ambiguity; 10bps RT; risk 1.5%; max 5",
        "frozen_baseline": BASE,
        "families": FAMILIES,
        "interpretation_rule": "Do not select best value. Look for a stable neighbourhood across full/OOS/recent windows.",
        "family_summary": sdf.to_dict("records"),
    }
    RESULTS_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print("\nROBUSTNESS SUMMARY — OOS")
    print("-" * 126)
    oos = sdf[sdf.window == "OOS_2023_TO_2024_08"]
    for r in oos.itertuples(index=False):
        print(
            f"{r.family:<24} all+={str(r.all_positive_return):5s} "
            f"allPF>1={str(r.all_pf_gt_1):5s} PF range={r.pf_min:.2f}-{r.pf_max:.2f} "
            f"Ret range={r.return_min:.2f}%..{r.return_max:.2f}% worstMDD={r.mdd_worst:.2f}%"
        )
    print("\nSaved parameter perturbation outputs.")


if __name__ == "__main__":
    main()
