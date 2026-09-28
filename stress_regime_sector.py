"""Regime + sector robustness stress for frozen SIDI validation snapshot.

Consumes SIDI_VALIDATION_SNAPSHOT_V1 only. No data is downloaded and no
strategy threshold is re-optimised.

Two questions:
1) Does the frozen SIDI edge survive across simple ex-ante market regimes?
2) Does portfolio performance depend on any single GICS sector?

To avoid relying on arbitrary candidate ordering when the five portfolio slots
are full, each scenario is evaluated over repeated reproducible randomized-slot
simulations. We report medians and P10/P90 distributions rather than selecting
any winner.
"""
from __future__ import annotations

import gzip
import json
import pickle
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

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


def run_scenario(snapshot, scenario_group, scenario_name, signal_map, signals_kept, rows):
    for wi, (window, start, end) in enumerate(WINDOWS):
        for i in range(N_SIMS):
            # Common-random-number design: same seed grid across scenarios.
            seed = BASE_SEED + wi * 100_000 + i
            st = slot.simulate_randomized(
                signal_map,
                snapshot["prices"],
                snapshot["indicators"],
                snapshot["current_fund_scores"],
                slot.REALISTIC,
                start,
                end,
                seed,
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
    print(
        f"Snapshot={snapshot['version']} created={snapshot.get('created_at_utc')} | "
        f"FULL baseline signals={base_kept} missing_context={missing}"
    )

    run_rows = []
    diag_rows = []

    # Baseline benchmark.
    run_scenario(snapshot, "BASELINE", "ALL_FULL", base_map, base_kept, run_rows)

    # Regimes are descriptive diagnostics, not candidate filters.
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
        run_scenario(snapshot, f"REGIME_{group}", name, smap, n, run_rows)

    # Calendar slices: enough to expose concentration in one historical year.
    for year in [2023, 2024, 2025, 2026]:
        smap, n = subset_signal_map(base_map, lambda t, d, s, y=year: str(d).startswith(str(y)))
        diag_rows.append({"type": "YEAR", "group": "YEAR", "scenario": str(year), "signals": n})
        # Only full-window run is informative for a year-specific map, but keeping the
        # same windows makes extraction consistent and transparently shows zeros where outside.
        run_scenario(snapshot, "REGIME_YEAR", f"YEAR_{year}", smap, n, run_rows)

    # Leave-one-sector-out. This asks whether any single sector is carrying the edge.
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
        removed = base_kept - n
        diag_rows.append({
            "type": "LEAVE_ONE_SECTOR_OUT", "group": "SECTOR", "scenario": sector,
            "signals": n, "signals_removed": removed,
        })
        run_scenario(snapshot, "LEAVE_ONE_SECTOR_OUT", f"EX_{sector}", smap, n, run_rows)

    rdf = pd.DataFrame(run_rows)
    rdf.to_csv(RESULTS_CSV, index=False)
    sdf = summarize(rdf)
    sdf.to_csv(SUMMARY_CSV, index=False)
    ddf = pd.DataFrame(diag_rows)
    ddf.to_csv(DETAIL_CSV, index=False)

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

    payload = {
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
    }
    RESULTS_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print("\nSaved regime + sector stress outputs.")


if __name__ == "__main__":
    main()
