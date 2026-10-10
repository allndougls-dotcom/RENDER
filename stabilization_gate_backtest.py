#!/usr/bin/env python3
"""
SIDI_INTRADAY_V2 · Stabilization Gate Confirmation Backtest

Frozen gates discovered in the prior pattern study:
A = range_pos60 >= 0.088
B = gap_pct >= -0.17
C = 0.848 <= volume_ratio_5_60 < 1.0

Tests BASE, A, B, C, AB, AC, BC, ABC with:
- independent episode statistics;
- fixed portfolio sizing: 1.5% per trade / 4.5% aggregate / 3 positions;
- backward holdout (pre-2022-09-12), not used to discover A/B/C;
- rolling 12-month windows stepped every 6 months.

This is experimental only. It does not alter production rules.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import build_indicators, download_prices, load_fundamental_scores, load_tickers
from pattern_backtest import (
    build_episode_dataset,
    enrich_signals,
    metrics as episode_metrics,
)
from risk_matrix_backtest import (
    build_signals,
    download_context_benchmarks,
    make_price_maps,
    norm_ticker,
    simulate_configuration,
)

STRATEGY_VERSION = "SIDI_INTRADAY_V2"
MODEL_VERSION = "SIDI_STABILIZATION_CONFIRMATION_V1"  # frozen A/B/C robustness

A_RANGE_POS60_MIN = 0.088
B_GAP_PCT_MIN = -0.17
C_VOL_RATIO_MIN = 0.848
C_VOL_RATIO_MAX = 1.0

PORTFOLIO_TRADE_RISK_PCT = 1.5
PORTFOLIO_RISK_CAP_PCT = 4.5
MAX_POSITIONS = 3

DISCOVERY_START = "2022-09-12"
OUT_DIR_DEFAULT = "data/stabilization_gate"

CONFIGS = {
    "BASE": (),
    "A": ("A",),
    "B": ("B",),
    "C": ("C",),
    "AB": ("A", "B"),
    "AC": ("A", "C"),
    "BC": ("B", "C"),
    "ABC": ("A", "B", "C"),
}


def gate_flags(sig):
    range_pos = float(sig.get("range_pos60", np.nan))
    gap = float(sig.get("gap_pct", np.nan))
    volr = float(sig.get("volume_ratio_5_60", np.nan))
    return {
        "A": bool(np.isfinite(range_pos) and range_pos >= A_RANGE_POS60_MIN),
        "B": bool(np.isfinite(gap) and gap >= B_GAP_PCT_MIN),
        "C": bool(np.isfinite(volr) and C_VOL_RATIO_MIN <= volr < C_VOL_RATIO_MAX),
    }


def passes_config(sig, config_name):
    flags = gate_flags(sig)
    return all(flags[g] for g in CONFIGS[config_name])


def filter_signals(enriched_signals, config_name, start_date=None, end_date=None):
    out = []
    for sig in enriched_signals:
        d = sig["entry_date"]
        if start_date is not None and d < start_date:
            continue
        if end_date is not None and d > end_date:
            continue
        if passes_config(sig, config_name):
            out.append(sig)
    return out


def to_entries_by_date(signals):
    out = {}
    for sig in signals:
        out.setdefault(sig["entry_date"], []).append(dict(sig))
    for _, rows in out.items():
        rows.sort(
            key=lambda s: (
                -float(s.get("fund_score", 0.0)),
                float(s.get("abnormal20", 0.0)),
                float(s.get("dd60", 0.0)),
                float(s.get("rsi", 0.0)),
                s.get("ticker", ""),
            )
        )
    return out


def slice_price_maps(price_maps, start_date, end_date):
    maps = {}
    dates = set()
    for ticker, per_date in price_maps.items():
        rows = {
            d: row for d, row in per_date.items()
            if start_date <= d <= end_date
        }
        if rows:
            maps[ticker] = rows
            dates.update(rows)
    return maps, sorted(dates)


def run_episode_config(signals, price_maps):
    trades, skipped = build_episode_dataset(signals, price_maps)
    m = episode_metrics(trades)
    return trades, skipped, m


def run_portfolio_config(signals, price_maps, all_dates):
    entries = to_entries_by_date(signals)
    summary, trades, equity = simulate_configuration(
        entries,
        price_maps,
        all_dates,
        PORTFOLIO_TRADE_RISK_PCT,
        PORTFOLIO_RISK_CAP_PCT,
    )
    return summary, trades, equity


def compare_metrics(base, other):
    def delta(key):
        a = other.get(key)
        b = base.get(key)
        if a is None or b is None:
            return np.nan
        try:
            return float(a) - float(b)
        except Exception:
            return np.nan

    return {
        "delta_avg_r": delta("avg_r"),
        "delta_win_rate": delta("win_rate"),
        "delta_profit_factor": delta("profit_factor"),
        "delta_hard_loss_rate": delta("hard_loss_rate"),
        "delta_gap_loss_rate": delta("gap_loss_rate"),
    }


def compare_portfolio(base, other):
    def delta(key):
        a = other.get(key)
        b = base.get(key)
        if a is None or b is None:
            return np.nan
        try:
            return float(a) - float(b)
        except Exception:
            return np.nan

    return {
        "delta_total_return_pct": delta("total_return_pct"),
        "delta_cagr_pct": delta("cagr_pct"),
        "delta_mdd_pct": delta("max_drawdown_pct"),
        "delta_calmar": delta("calmar"),
        "delta_sharpe": delta("sharpe"),
        "delta_trades": delta("trades"),
    }


def period_result(name, signals, price_maps, all_dates):
    episode_trades, skipped, em = run_episode_config(signals, price_maps)
    pm, portfolio_trades, equity = run_portfolio_config(
        signals, price_maps, all_dates
    )
    return {
        "config": name,
        "signals_after_gate": len(signals),
        "episodes": len(episode_trades),
        "same_ticker_overlap_skipped": skipped,
        "episode_metrics": em,
        "portfolio_metrics": pm,
        "portfolio_trade_count": len(portfolio_trades),
    }


def build_rolling_windows(first_date, last_date):
    first = pd.Timestamp(first_date)
    last = pd.Timestamp(last_date)
    # Start at the first Jan/Jul after sufficient context exists.
    cursor = pd.Timestamp(year=first.year, month=1 if first.month <= 6 else 7, day=1)
    if cursor < first:
        cursor = cursor + pd.DateOffset(months=6)
    windows = []
    while cursor <= last:
        end = cursor + pd.DateOffset(years=1) - pd.Timedelta(days=1)
        if end > last:
            break
        windows.append((
            cursor.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
            f"{cursor.strftime('%Y-%m')}_{end.strftime('%Y-%m')}",
        ))
        cursor = cursor + pd.DateOffset(months=6)
    return windows


def flat_summary_row(scope, config_name, result, base_result):
    em = result["episode_metrics"]
    pm = result["portfolio_metrics"]
    bem = base_result["episode_metrics"]
    bpm = base_result["portfolio_metrics"]
    ec = compare_metrics(bem, em)
    pc = compare_portfolio(bpm, pm)
    return {
        "scope": scope,
        "config": config_name,
        "signals_after_gate": result["signals_after_gate"],
        "episodes": result["episodes"],
        "keep_rate_vs_base_episodes": (
            result["episodes"] / base_result["episodes"]
            if base_result["episodes"] else np.nan
        ),
        "avg_r": em.get("avg_r"),
        "win_rate": em.get("win_rate"),
        "profit_factor": em.get("profit_factor"),
        "hard_loss_rate": em.get("hard_loss_rate"),
        "gap_loss_rate": em.get("gap_loss_rate"),
        **ec,
        "portfolio_trades": pm.get("trades"),
        "portfolio_return_pct": pm.get("total_return_pct"),
        "portfolio_cagr_pct": pm.get("cagr_pct"),
        "portfolio_mdd_pct": pm.get("max_drawdown_pct"),
        "portfolio_calmar": pm.get("calmar"),
        "portfolio_sharpe": pm.get("sharpe"),
        **pc,
    }


def json_clean(obj):
    if isinstance(obj, dict):
        return {str(k): json_clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if not np.isfinite(obj) else float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    return obj


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=int, default=8)
    p.add_argument("--out-dir", default=OUT_DIR_DEFAULT)
    args = p.parse_args()

    print("=" * 88)
    print("SIDI_INTRADAY_V2 · STABILIZATION GATE CONFIRMATION")
    print(f"Modelo: {MODEL_VERSION}")
    print(
        f"A range_pos60>={A_RANGE_POS60_MIN} · "
        f"B gap>={B_GAP_PCT_MIN}% · "
        f"C {C_VOL_RATIO_MIN}<=vol5/60<{C_VOL_RATIO_MAX}"
    )
    print(
        f"Portfolio frozen: {PORTFOLIO_TRADE_RISK_PCT}% trade · "
        f"{PORTFOLIO_RISK_CAP_PCT}% cap · {MAX_POSITIONS} slots"
    )
    print("=" * 88)

    all_tickers = [norm_ticker(t) for t in load_tickers()]
    fund_scores = {norm_ticker(t): v for t, v in load_fundamental_scores().items()}
    tickers = [
        t for t in all_tickers
        if t in fund_scores and float(fund_scores[t].get("fund_score", 0.0)) >= 6.5
    ]
    print(f"Universo tras gate fundamental >=6.5: {len(tickers)}/{len(all_tickers)}")

    prices = {
        norm_ticker(t): df
        for t, df in download_prices(tickers, years=args.years).items()
    }
    if not prices:
        raise RuntimeError("No hay precios")

    indicators = build_indicators(prices)
    benchmarks = download_context_benchmarks(args.years)
    entries_by_date, signal_counts = build_signals(
        prices, indicators, fund_scores, benchmarks
    )
    price_maps, all_dates = make_price_maps(prices)
    enriched = enrich_signals(
        entries_by_date, prices, indicators, fund_scores, benchmarks
    )
    if len(enriched) < 250:
        raise RuntimeError(f"Muestra FULL insuficiente: {len(enriched)}")

    first_signal = min(s["entry_date"] for s in enriched)
    last_signal = max(s["entry_date"] for s in enriched)
    discovery_start = max(DISCOVERY_START, first_signal)

    # Global 8y sample.
    global_results = {}
    global_rows = []
    for name in CONFIGS:
        sigs = filter_signals(enriched, name)
        global_results[name] = period_result(name, sigs, price_maps, all_dates)
    for name in CONFIGS:
        global_rows.append(
            flat_summary_row(
                "GLOBAL_8Y", name, global_results[name], global_results["BASE"]
            )
        )

    # Backward holdout: before the 4y discovery era.
    holdout_end = (
        pd.Timestamp(discovery_start) - pd.Timedelta(days=1)
    ).strftime("%Y-%m-%d")
    holdout_maps, holdout_dates = slice_price_maps(
        price_maps, first_signal, holdout_end
    )
    holdout_results = {}
    holdout_rows = []
    if holdout_dates:
        for name in CONFIGS:
            sigs = filter_signals(
                enriched, name, start_date=first_signal, end_date=holdout_end
            )
            holdout_results[name] = period_result(
                name, sigs, holdout_maps, holdout_dates
            )
        for name in CONFIGS:
            holdout_rows.append(
                flat_summary_row(
                    "BACKWARD_HOLDOUT",
                    name,
                    holdout_results[name],
                    holdout_results["BASE"],
                )
            )

    # Discovery-era confirmation, same 2022-2026 era as prior study.
    recent_maps, recent_dates = slice_price_maps(
        price_maps, discovery_start, last_signal
    )
    recent_results = {}
    recent_rows = []
    for name in CONFIGS:
        sigs = filter_signals(
            enriched, name, start_date=discovery_start, end_date=last_signal
        )
        recent_results[name] = period_result(
            name, sigs, recent_maps, recent_dates
        )
    for name in CONFIGS:
        recent_rows.append(
            flat_summary_row(
                "DISCOVERY_ERA_2022_2026",
                name,
                recent_results[name],
                recent_results["BASE"],
            )
        )

    # Rolling 12m windows, stepped 6m.
    rolling_rows = []
    rolling_windows = build_rolling_windows(first_signal, last_signal)
    for start, end, label in rolling_windows:
        wmaps, wdates = slice_price_maps(price_maps, start, end)
        if len(wdates) < 100:
            continue
        window_results = {}
        for name in CONFIGS:
            sigs = filter_signals(
                enriched, name, start_date=start, end_date=end
            )
            if len(sigs) < 5 and name != "BASE":
                continue
            window_results[name] = period_result(
                name, sigs, wmaps, wdates
            )
        if "BASE" not in window_results:
            continue
        if window_results["BASE"]["episodes"] < 12:
            continue
        for name, result in window_results.items():
            row = flat_summary_row(
                f"ROLLING_{label}", name, result, window_results["BASE"]
            )
            row["window_start"] = start
            row["window_end"] = end
            rolling_rows.append(row)

    summary_df = pd.DataFrame(global_rows + holdout_rows + recent_rows)
    rolling_df = pd.DataFrame(rolling_rows)

    # Robustness score across rolling windows.
    robustness_rows = []
    for name in CONFIGS:
        if name == "BASE":
            continue
        sub = rolling_df[rolling_df["config"] == name].copy()
        if len(sub) == 0:
            continue
        valid_avg = sub["delta_avg_r"].dropna()
        valid_pf = sub["delta_profit_factor"].dropna()
        valid_ret = sub["delta_total_return_pct"].dropna()
        valid_dd = sub["delta_mdd_pct"].dropna()
        hold = next(
            (r for r in holdout_rows if r["config"] == name),
            None,
        )
        recent = next(
            (r for r in recent_rows if r["config"] == name),
            None,
        )
        robustness_rows.append({
            "config": name,
            "rolling_windows": int(len(sub)),
            "pct_windows_avg_r_improves": float((valid_avg > 0).mean() * 100.0) if len(valid_avg) else np.nan,
            "pct_windows_pf_improves": float((valid_pf > 0).mean() * 100.0) if len(valid_pf) else np.nan,
            "pct_windows_return_improves": float((valid_ret > 0).mean() * 100.0) if len(valid_ret) else np.nan,
            # MDD is negative, so positive delta means less-negative / improved.
            "pct_windows_mdd_improves": float((valid_dd > 0).mean() * 100.0) if len(valid_dd) else np.nan,
            "median_delta_avg_r": float(valid_avg.median()) if len(valid_avg) else np.nan,
            "median_delta_pf": float(valid_pf.median()) if len(valid_pf) else np.nan,
            "median_delta_return_pct": float(valid_ret.median()) if len(valid_ret) else np.nan,
            "median_delta_mdd_pct": float(valid_dd.median()) if len(valid_dd) else np.nan,
            "holdout_delta_avg_r": hold["delta_avg_r"] if hold else np.nan,
            "holdout_delta_pf": hold["delta_profit_factor"] if hold else np.nan,
            "holdout_delta_return_pct": hold["delta_total_return_pct"] if hold else np.nan,
            "holdout_delta_mdd_pct": hold["delta_mdd_pct"] if hold else np.nan,
            "recent_delta_avg_r": recent["delta_avg_r"] if recent else np.nan,
            "recent_delta_pf": recent["delta_profit_factor"] if recent else np.nan,
            "recent_delta_return_pct": recent["delta_total_return_pct"] if recent else np.nan,
            "recent_delta_mdd_pct": recent["delta_mdd_pct"] if recent else np.nan,
            "holdout_episode_keep_rate": hold["keep_rate_vs_base_episodes"] if hold else np.nan,
            "recent_episode_keep_rate": recent["keep_rate_vs_base_episodes"] if recent else np.nan,
        })

    robustness_df = pd.DataFrame(robustness_rows)
    if len(robustness_df):
        robustness_df["robust_score"] = (
            (robustness_df["pct_windows_avg_r_improves"] >= 60).astype(int)
            + (robustness_df["pct_windows_pf_improves"] >= 60).astype(int)
            + (robustness_df["holdout_delta_avg_r"] > 0).astype(int)
            + (robustness_df["holdout_delta_pf"] > 0).astype(int)
            + (robustness_df["recent_delta_avg_r"] > 0).astype(int)
            + (robustness_df["recent_delta_pf"] > 0).astype(int)
        )
        robustness_df = robustness_df.sort_values(
            [
                "robust_score",
                "pct_windows_avg_r_improves",
                "median_delta_avg_r",
            ],
            ascending=[False, False, False],
        ).reset_index(drop=True)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary_df.to_csv(out / "stabilization_summary.csv", index=False)
    rolling_df.to_csv(out / "stabilization_walkforward.csv", index=False)
    robustness_df.to_csv(out / "stabilization_robustness.csv", index=False)

    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "strategy_version": STRATEGY_VERSION,
        "model_version": MODEL_VERSION,
        "frozen_gates": {
            "A": f"range_pos60 >= {A_RANGE_POS60_MIN}",
            "B": f"gap_pct >= {B_GAP_PCT_MIN}",
            "C": f"{C_VOL_RATIO_MIN} <= volume_ratio_5_60 < {C_VOL_RATIO_MAX}",
        },
        "frozen_portfolio": {
            "trade_risk_pct": PORTFOLIO_TRADE_RISK_PCT,
            "portfolio_risk_cap_pct": PORTFOLIO_RISK_CAP_PCT,
            "max_positions": MAX_POSITIONS,
        },
        "methodology": {
            "configs": list(CONFIGS),
            "years_requested": args.years,
            "first_signal": first_signal,
            "last_signal": last_signal,
            "backward_holdout_end": holdout_end,
            "discovery_era_start": discovery_start,
            "rolling_windows": "12 months, step 6 months, minimum 12 baseline episodes",
            "fund_score_warning": "Current fund_score applied retroactively; same known proxy limitation as prior backtests.",
            "threshold_warning": "A/B/C were selected from 2022-2026 evidence. The pre-2022 period is the only temporally unseen historical holdout in this confirmation.",
        },
        "signal_counts": signal_counts,
        "full_signals_enriched": len(enriched),
        "global": global_results,
        "backward_holdout": holdout_results,
        "discovery_era": recent_results,
        "robustness_ranking": robustness_df.to_dict("records"),
    }
    with open(out / "stabilization_report.json", "w", encoding="utf-8") as fh:
        json.dump(json_clean(report), fh, ensure_ascii=False, indent=2)

    print("\nROBUSTNESS RANKING")
    if len(robustness_df):
        cols = [
            "config", "robust_score",
            "pct_windows_avg_r_improves", "pct_windows_pf_improves",
            "median_delta_avg_r", "median_delta_pf",
            "holdout_delta_avg_r", "holdout_delta_pf",
            "recent_delta_avg_r", "recent_delta_pf",
        ]
        print(robustness_df[cols].to_string(index=False))
    print(f"\nResultados: {out}")


if __name__ == "__main__":
    main()
