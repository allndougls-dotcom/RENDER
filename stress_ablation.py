"""Final one-rule-at-a-time ablation stress for SIDI.

Uses SIDI_ABLATION_SNAPSHOT_V1. Each variant removes exactly one rule while
keeping every other rule, execution assumption and portfolio constraint fixed.
Slot allocation is randomized reproducibly when the 5-position portfolio is
oversubscribed.

This is diagnostic, not optimisation: do not choose a new parameter from it.
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
import stress_regime_sector as reg

SNAPSHOT_PATH = Path("sidi_ablation_snapshot_v1.pkl.gz")
RESULTS_CSV = Path("sidi_ablation_runs.csv")
SUMMARY_CSV = Path("sidi_ablation_summary.csv")
RESULTS_JSON = Path("sidi_ablation.json")

N_SIMS = 250
BASE_SEED = 2026092801
WINDOWS = [
    ("FULL_2023_2026", "2023-01-01", "2026-09-10"),
    ("OOS_2023_TO_2024_08", "2023-01-01", "2024-08-10"),
    ("RECENT_2025_09_TO_2026", "2025-09-01", "2026-09-10"),
]
VARIANTS = [
    "BASELINE",
    "NO_FUND",
    "NO_SPY20",
    "NO_ABNORMAL20",
    "NO_DD12",
    "NO_RSI40",
    "NO_MACD_IMPROVING",
    "NO_VOLUME_DECLINING",
]


def present(x):
    return x is not None and pd.notna(x) and np.isfinite(float(x))


def build_variant_map(snapshot, variant):
    out = {}
    missing_context = 0
    rejected = {
        "coverage": 0,
        "fund": 0,
        "spy": 0,
        "abnormal": 0,
        "rsi": 0,
        "dd": 0,
        "macd": 0,
        "volume": 0,
    }
    for ticker, sigs in snapshot["signals"].items():
        for dte, sig in sigs.items():
            if int(sig.get("pit_coverage", 0)) < 6:
                rejected["coverage"] += 1
                continue
            ctx = snapshot["context"].get((ticker, dte), {})
            spy20 = ctx.get("spy20")
            abnormal20 = ctx.get("abnormal20")
            if not present(spy20) or not present(abnormal20):
                missing_context += 1
                continue

            if variant != "NO_RSI40" and not bool(sig.get("rsi_ok")):
                rejected["rsi"] += 1
                continue
            if variant != "NO_DD12" and not bool(sig.get("dd_ok")):
                rejected["dd"] += 1
                continue
            if variant != "NO_MACD_IMPROVING" and not bool(sig.get("macd_ok")):
                rejected["macd"] += 1
                continue
            if variant != "NO_VOLUME_DECLINING" and not bool(sig.get("volume_ok")):
                rejected["volume"] += 1
                continue

            if variant != "NO_FUND" and float(sig["pit_fund_score"]) < 6.66:
                rejected["fund"] += 1
                continue
            if variant != "NO_SPY20" and float(spy20) > 1.0:
                rejected["spy"] += 1
                continue
            if variant != "NO_ABNORMAL20" and float(abnormal20) > -10.0:
                rejected["abnormal"] += 1
                continue

            item = dict(sig)
            item["shadow_spy20"] = float(spy20)
            item["shadow_abnormal20"] = float(abnormal20)
            out.setdefault(ticker, {})[dte] = item

    return out, sum(len(v) for v in out.values()), missing_context, rejected


def qstats(values):
    s = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if s.empty:
        return {"min": np.nan, "p10": np.nan, "median": np.nan, "p90": np.nan, "max": np.nan}
    return {
        "min": float(s.min()),
        "p10": float(s.quantile(.10)),
        "median": float(s.median()),
        "p90": float(s.quantile(.90)),
        "max": float(s.max()),
    }


def main():
    print("=" * 132)
    print("SIDI FINAL ABLATION STRESS — ONE RULE REMOVED AT A TIME")
    print("=" * 132)
    print(f"{N_SIMS} randomized-slot simulations per variant/window. Execution remains gap-real + 10bps, risk 1.5%, max 5.\n")

    with gzip.open(SNAPSHOT_PATH, "rb") as f:
        snap = pickle.load(f)
    if snap.get("version") != "SIDI_ABLATION_SNAPSHOT_V1":
        raise RuntimeError(f"Unexpected snapshot version: {snap.get('version')}")

    prices = snap["prices"]
    indicators = snap["indicators"]
    fund_scores = snap["current_fund_scores"]
    rows_by_date = exp.price_indexes(prices)
    master_dates = sorted({d for rows in rows_by_date.values() for d in rows})

    variant_maps = {}
    diagnostics = {}
    for variant in VARIANTS:
        smap, n, missing, rejected = build_variant_map(snap, variant)
        variant_maps[variant] = smap
        diagnostics[variant] = {
            "signals_full_period": n,
            "missing_context": missing,
            "rejected": rejected,
        }

    rows = []
    summaries = []
    for wi, (wname, start, end) in enumerate(WINDOWS):
        print(f"\n{wname}: {start} -> {end}")
        print("-" * 132)
        for vi, variant in enumerate(VARIANTS):
            smap = variant_maps[variant]
            scheduled = exp.schedule_entries(smap, indicators)
            sims = []
            for i in range(N_SIMS):
                seed = BASE_SEED + wi * 100_000 + i
                st = reg.simulate_fast(
                    smap, scheduled, rows_by_date, master_dates,
                    fund_scores, start, end, seed,
                )
                row = {"variant": variant, "window": wname, **st}
                rows.append(row)
                sims.append(st)

            sdf = pd.DataFrame(sims)
            pf = qstats(sdf["profit_factor"])
            ret = qstats(sdf["total_return"])
            mdd = qstats(sdf["max_drawdown_mtm"])
            trades = qstats(sdf["trades"])
            prob_pf12 = float((sdf["profit_factor"] >= 1.2).mean() * 100)
            prob_pos = float((sdf["total_return"] > 0).mean() * 100)
            summary = {
                "variant": variant,
                "window": wname,
                "signals_full_period": diagnostics[variant]["signals_full_period"],
                "pf_p10": pf["p10"],
                "pf_median": pf["median"],
                "pf_p90": pf["p90"],
                "return_p10": ret["p10"],
                "return_median": ret["median"],
                "return_p90": ret["p90"],
                "mdd_p10": mdd["p10"],
                "mdd_median": mdd["median"],
                "mdd_p90": mdd["p90"],
                "trades_median": trades["median"],
                "prob_pf_ge_1_2_pct": prob_pf12,
                "prob_return_positive_pct": prob_pos,
            }
            summaries.append(summary)
            print(
                f"{variant:<24} sig={diagnostics[variant]['signals_full_period']:5d} "
                f"trades~{trades['median']:5.0f} PF p10/med/p90={pf['p10']:.2f}/{pf['median']:.2f}/{pf['p90']:.2f} "
                f"Ret med={ret['median']:7.2f}% MDD med={mdd['median']:7.2f}% P(PF>=1.2)={prob_pf12:5.1f}%"
            )

    rdf = pd.DataFrame(rows)
    sdf = pd.DataFrame(summaries)

    # Add deltas versus baseline within each validation window.
    for wname, _, _ in WINDOWS:
        mask = sdf["window"] == wname
        base = sdf[mask & (sdf["variant"] == "BASELINE")].iloc[0]
        sdf.loc[mask, "delta_pf_vs_baseline"] = sdf.loc[mask, "pf_median"] - float(base["pf_median"])
        sdf.loc[mask, "delta_return_vs_baseline"] = sdf.loc[mask, "return_median"] - float(base["return_median"])
        sdf.loc[mask, "delta_mdd_vs_baseline"] = sdf.loc[mask, "mdd_median"] - float(base["mdd_median"])

    rdf.to_csv(RESULTS_CSV, index=False)
    sdf.to_csv(SUMMARY_CSV, index=False)
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "snapshot_version": snap["version"],
        "source_snapshot_version": snap.get("source_snapshot_version"),
        "method": "remove exactly one rule; keep PIT coverage>=6 for sample comparability; randomized 5-slot allocation",
        "execution": "T+1 open, gap-aware SL, stop-first same-bar ambiguity, 10bps RT, risk 1.5%, max 5, TP .75ATR, SL -5%, T7",
        "variants": VARIANTS,
        "diagnostics": diagnostics,
        "summary": sdf.to_dict("records"),
        "interpretation": "If removing a rule degrades PF/return or worsens MDD consistently, it adds useful edge. If removal is neutral/improves across windows, the rule may be redundant; do not change production from this test alone.",
    }
    RESULTS_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print("\nSaved final ablation outputs.")


if __name__ == "__main__":
    main()
