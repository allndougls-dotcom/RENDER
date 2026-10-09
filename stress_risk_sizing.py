"""SIDI risk sizing stress — per-trade risk x aggregate portfolio risk.

Frozen strategy and data:
- SIDI_VALIDATION_SNAPSHOT_V1
- DD60 >= 12%, PIT fund proxy >= 6.66, coverage >= 6
- RSI<40 + MACD hist improving + declining volume
- SPY20 <= +1%, abnormal20 <= -10%
- T+1 OPEN, TP=0.75*ATR14(signal), SL=-5%, T7
- gap-aware stop, STOP-first same-bar ambiguity, 10 bps round-trip costs
- max 5 positions

Only sizing changes. This is not a signal/parameter optimisation.

Portfolio risk cap = sum of the INITIAL EUR risk budgets of open positions.
A new trade enters only if its full requested per-trade risk fits below the
aggregate cap. Positions are never partially sized just to fill spare budget.
"""
from __future__ import annotations

import gzip
import json
import math
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
RUNS_CSV = Path("sidi_risk_sizing_runs.csv")
SUMMARY_CSV = Path("sidi_risk_sizing_summary.csv")
JSON_PATH = Path("sidi_risk_sizing.json")

N_SIMS = 250
BASE_SEED = 20261009
MAX_POSITIONS = 5

RISK_PER_TRADE = [0.015, 0.020, 0.025, 0.030]
PORTFOLIO_RISK_CAP = [0.075, 0.100, 0.125, 0.150]

WINDOWS = [
    ("FULL_2023_2026", "2023-01-01", "2026-09-10"),
    ("OOS_2023_TO_2024_08", "2023-01-01", "2024-08-10"),
    ("RECENT_2025_09_TO_2026", "2025-09-01", "2026-09-10"),
]


def load_snapshot():
    with gzip.open(SNAPSHOT_PATH, "rb") as f:
        snap = pickle.load(f)
    if snap.get("version") != "SIDI_VALIDATION_SNAPSHOT_V1":
        raise RuntimeError(f"Unexpected snapshot version: {snap.get('version')}")
    return snap


def qstats(values):
    x = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    if x.empty:
        return {k: np.nan for k in ["min","p10","median","p90","max","mean"]}
    return {
        "min": float(x.min()),
        "p10": float(x.quantile(.10)),
        "median": float(x.median()),
        "p90": float(x.quantile(.90)),
        "max": float(x.max()),
        "mean": float(x.mean()),
    }


def stats_for(trades, equity_curve, final_capital, initial_capital=10_000.0):
    df = pd.DataFrame(trades)
    eq = pd.DataFrame(equity_curve)

    if len(df):
        wins = df["pnl_eur"] > 0
        gross_profit = float(df.loc[wins, "pnl_eur"].sum())
        gross_loss = abs(float(df.loc[~wins, "pnl_eur"].sum()))
        pf = gross_profit / gross_loss if gross_loss > 0 else 999.0
        wr = float(wins.mean() * 100)
        avg_days = float(df["days"].mean())
    else:
        pf = wr = avg_days = 0.0

    if len(eq):
        s = eq["equity"].astype(float)
        mdd = float(((s / s.cummax()) - 1.0).min() * 100.0)
    else:
        mdd = 0.0

    ret = (final_capital / initial_capital - 1.0) * 100.0
    return {
        "trades": int(len(df)),
        "win_rate": wr,
        "profit_factor": pf,
        "total_return": ret,
        "max_drawdown_mtm": mdd,
        "avg_days": avg_days,
        "final_capital": final_capital,
    }


def simulate(
    signal_map, scheduled, rows_by_date, master_dates, fund_scores,
    start, end, seed, risk_trade, portfolio_cap
):
    rng = np.random.default_rng(seed)
    dates = [d for d in master_dates if start <= d <= end]
    scenario = slot.REALISTIC

    capital = exp.INITIAL_CAPITAL
    open_pos = []
    trades = []
    equity_curve = []

    slip = float(scenario.slippage_bps) / 10_000.0
    cost_rate = float(scenario.cost_bps_rt) / 10_000.0

    gap_stops = 0
    same_bar_stops = 0
    oversubscribed_days = 0
    skipped_due_slots = 0
    skipped_due_risk_cap = 0
    max_open_risk_pct = 0.0
    sum_open_risk_pct = 0.0
    risk_obs = 0

    def pnl_net(pos, exit_price):
        pnl_pct = (exit_price - pos["entry"]) / pos["entry"]
        gross = pos["risk_eur"] * (pnl_pct / pos["stop_distance_pct"])
        notional = pos["risk_eur"] / pos["stop_distance_pct"]
        cost = notional * cost_rate
        return pnl_pct, gross - cost

    def close_position(pos, date, reason, exit_price):
        nonlocal capital, same_bar_stops
        pnl_pct, pnl_eur = pnl_net(pos, exit_price)
        capital += pnl_eur
        if reason == "STOP_BOTH_TOUCHED":
            same_bar_stops += 1
        trades.append({
            "ticker": pos["ticker"],
            "signal_date": pos["signal_date"],
            "entry_date": pos["entry_date"],
            "exit_date": date,
            "days": pos["days_held"],
            "exit_reason": reason,
            "pnl_pct": pnl_pct * 100.0,
            "pnl_eur": pnl_eur,
            "risk_eur": pos["risk_eur"],
        })

    def evaluate(pos, row, allow_time_stop=True, existing_position=True):
        nonlocal gap_stops
        op = float(row["Open"])
        hi = float(row["High"])
        lo = float(row["Low"])
        cl = float(row["Close"])

        if existing_position and op <= pos["stop"]:
            gap_stops += 1
            return "STOP_GAP", op * (1.0 - slip)

        stop_hit = lo <= pos["stop"]
        target_hit = hi >= pos["target"]
        stop_fill = pos["stop"] * (1.0 - slip)
        if stop_hit and target_hit:
            return "STOP_BOTH_TOUCHED", stop_fill
        if stop_hit:
            return "STOP", stop_fill
        if target_hit:
            return "TARGET", pos["target"]
        if allow_time_stop and pos["days_held"] >= pit_base.CANDIDATE.time_stop:
            return "TIME_STOP", cl * (1.0 - slip)
        return None, None

    for date in dates:
        # Existing positions first.
        survivors = []
        for pos in open_pos:
            row = rows_by_date.get(pos["ticker"], {}).get(date)
            if row is None:
                survivors.append(pos)
                continue
            pos["days_held"] += 1
            reason, exit_px = evaluate(pos, row, True, True)
            if reason:
                close_position(pos, date, reason, float(exit_px))
            else:
                survivors.append(pos)
        open_pos = survivors

        open_tickers = {p["ticker"] for p in open_pos}
        candidates = [s for s in scheduled.get(date, []) if s["ticker"] not in open_tickers]

        requested_risk = capital * risk_trade
        cap_eur = capital * portfolio_cap
        open_risk = sum(float(p["risk_eur"]) for p in open_pos)
        remaining_slots = max(0, MAX_POSITIONS - len(open_pos))
        if requested_risk > 0:
            risk_slots = max(0, int(math.floor((cap_eur - open_risk + 1e-9) / requested_risk)))
        else:
            risk_slots = 0
        entries_possible = min(remaining_slots, risk_slots)

        if len(candidates) > entries_possible:
            oversubscribed_days += 1
            order = rng.permutation(len(candidates))
            candidates = [candidates[i] for i in order]

        skipped_due_slots += max(0, len(candidates) - remaining_slots)
        if remaining_slots > 0:
            skipped_due_risk_cap += max(0, min(len(candidates), remaining_slots) - entries_possible)

        for sig in candidates[:entries_possible]:
            ticker = sig["ticker"]
            row = rows_by_date.get(ticker, {}).get(date)
            if row is None or pd.isna(row.get("Open", np.nan)):
                continue

            entry = float(row["Open"]) * (1.0 + slip)
            atr = float(sig.get("atr_exact", 0.0))
            target, stop, tp_pct, stop_dist_pct = exp.target_and_stop(
                entry, atr, pit_base.CANDIDATE
            )
            if stop_dist_pct <= 0:
                continue

            risk_eur = capital * risk_trade
            # Re-check cap because same-day immediate exits can alter capital.
            current_open_risk = sum(float(p["risk_eur"]) for p in open_pos)
            if current_open_risk + risk_eur > capital * portfolio_cap + 1e-9:
                skipped_due_risk_cap += 1
                continue

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
                "risk_eur": risk_eur,
                "rsi": float(sig["rsi"]),
                "dd": float(sig["dd"]),
                "fund_score": float(fund_scores.get(ticker, {}).get("fund_score", 0.0)),
                "days_held": 1,
            }

            reason, exit_px = evaluate(
                pos, row,
                allow_time_stop=(pit_base.CANDIDATE.time_stop <= 1),
                existing_position=False,
            )
            if reason:
                close_position(pos, date, reason, float(exit_px))
            else:
                open_pos.append(pos)

        open_risk = sum(float(p["risk_eur"]) for p in open_pos)
        open_risk_pct = (open_risk / capital * 100.0) if capital > 0 else 0.0
        max_open_risk_pct = max(max_open_risk_pct, open_risk_pct)
        sum_open_risk_pct += open_risk_pct
        risk_obs += 1

        mtm = capital
        for pos in open_pos:
            row = rows_by_date.get(pos["ticker"], {}).get(date)
            if row is None:
                continue
            cl = float(row["Close"])
            pnl_pct = (cl - pos["entry"]) / pos["entry"]
            mtm += pos["risk_eur"] * (pnl_pct / pos["stop_distance_pct"])
        equity_curve.append({"date": date, "equity": mtm})

    # Close residual positions at period end.
    final_date = dates[-1] if dates else end
    for pos in list(open_pos):
        row = rows_by_date.get(pos["ticker"], {}).get(final_date)
        d = final_date
        if row is None:
            td = [x for x in rows_by_date.get(pos["ticker"], {}) if x <= end]
            if not td:
                continue
            d = max(td)
            row = rows_by_date[pos["ticker"]][d]
        close_position(pos, d, "END_OF_PERIOD", float(row["Close"]) * (1.0 - slip))

    st = stats_for(trades, equity_curve, capital)
    st.update({
        "seed": int(seed),
        "risk_per_trade_pct": risk_trade * 100.0,
        "portfolio_risk_cap_pct": portfolio_cap * 100.0,
        "max_positions": MAX_POSITIONS,
        "max_open_risk_pct": max_open_risk_pct,
        "avg_open_risk_pct": (sum_open_risk_pct / risk_obs) if risk_obs else 0.0,
        "oversubscribed_days": oversubscribed_days,
        "skipped_due_slots": skipped_due_slots,
        "skipped_due_risk_cap": skipped_due_risk_cap,
        "gap_stop_count": gap_stops,
        "same_bar_stop_count": same_bar_stops,
    })
    return st


def summarize(df):
    rows = []
    for (rt, pc, window), g in df.groupby(
        ["risk_per_trade_pct", "portfolio_risk_cap_pct", "window"]
    ):
        ret = qstats(g["total_return"])
        mdd = qstats(g["max_drawdown_mtm"])
        pf = qstats(g["profit_factor"])
        trades = qstats(g["trades"])
        rows.append({
            "risk_per_trade_pct": rt,
            "portfolio_risk_cap_pct": pc,
            "window": window,
            "simulations": len(g),
            "trades_median": trades["median"],
            "pf_p10": pf["p10"],
            "pf_median": pf["median"],
            "pf_p90": pf["p90"],
            "return_p10": ret["p10"],
            "return_median": ret["median"],
            "return_p90": ret["p90"],
            "mdd_p10": mdd["p10"],
            "mdd_median": mdd["median"],
            "mdd_p90": mdd["p90"],
            "max_open_risk_median": float(g["max_open_risk_pct"].median()),
            "skipped_risk_cap_median": float(g["skipped_due_risk_cap"].median()),
            "prob_return_positive_pct": float((g["total_return"] > 0).mean() * 100.0),
            "prob_mdd_le_minus15_pct": float((g["max_drawdown_mtm"] <= -15).mean() * 100.0),
            "prob_mdd_le_minus20_pct": float((g["max_drawdown_mtm"] <= -20).mean() * 100.0),
            "prob_mdd_le_minus25_pct": float((g["max_drawdown_mtm"] <= -25).mean() * 100.0),
            "prob_mdd_le_minus30_pct": float((g["max_drawdown_mtm"] <= -30).mean() * 100.0),
            "return_to_mdd_median": (
                float(g["total_return"].median()) / abs(float(g["max_drawdown_mtm"].median()))
                if float(g["max_drawdown_mtm"].median()) != 0 else np.nan
            ),
        })
    return pd.DataFrame(rows)


def main():
    print("=" * 136)
    print("SIDI RISK SIZING STRESS — PER-TRADE RISK x AGGREGATE PORTFOLIO RISK")
    print("=" * 136)
    print(
        f"{N_SIMS} randomized-slot simulations per combination/window | "
        f"risk/trade={[(x*100) for x in RISK_PER_TRADE]} | "
        f"portfolio caps={[(x*100) for x in PORTFOLIO_RISK_CAP]}\n"
    )

    snap = load_snapshot()
    signal_map, kept, missing = pert.build_signal_map(snap, pert.BASE)
    rows_by_date = exp.price_indexes(snap["prices"])
    scheduled = exp.schedule_entries(signal_map, snap["indicators"])
    master_dates = sorted({d for rows in rows_by_date.values() for d in rows})
    print(
        f"Snapshot={snap['version']} created={snap.get('created_at_utc')} | "
        f"FULL signals={kept} missing_context={missing}"
    )

    run_rows = []
    for ri, risk_trade in enumerate(RISK_PER_TRADE):
        for ci, portfolio_cap in enumerate(PORTFOLIO_RISK_CAP):
            label = f"{risk_trade*100:.1f}%/trade | {portfolio_cap*100:.1f}% portfolio"
            print("\n" + label)
            print("-" * 136)
            for wi, (window, start, end) in enumerate(WINDOWS):
                wr = []
                for i in range(N_SIMS):
                    seed = BASE_SEED + ri*1_000_000 + ci*100_000 + wi*10_000 + i
                    st = simulate(
                        signal_map, scheduled, rows_by_date, master_dates,
                        snap["current_fund_scores"], start, end, seed,
                        risk_trade, portfolio_cap,
                    )
                    st["window"] = window
                    run_rows.append(st)
                    wr.append(st)
                wdf = pd.DataFrame(wr)
                print(
                    f"  {window:<24} trades~{wdf.trades.median():5.0f} "
                    f"PF={wdf.profit_factor.median():.2f} "
                    f"Ret p10/med/p90={wdf.total_return.quantile(.10):7.2f}%/"
                    f"{wdf.total_return.median():7.2f}%/{wdf.total_return.quantile(.90):7.2f}% "
                    f"MDD p10/med/p90={wdf.max_drawdown_mtm.quantile(.10):7.2f}%/"
                    f"{wdf.max_drawdown_mtm.median():7.2f}%/{wdf.max_drawdown_mtm.quantile(.90):7.2f}%"
                )

    rdf = pd.DataFrame(run_rows)
    rdf.to_csv(RUNS_CSV, index=False)
    sdf = summarize(rdf)
    sdf.to_csv(SUMMARY_CSV, index=False)

    full = sdf[sdf.window == "FULL_2023_2026"].copy()
    print("\nFULL-WINDOW RISK MATRIX — MEDIANS")
    print("-" * 136)
    for r in full.itertuples(index=False):
        print(
            f"risk/trade={r.risk_per_trade_pct:4.1f}% cap={r.portfolio_risk_cap_pct:4.1f}% | "
            f"Ret={r.return_median:8.2f}% MDD={r.mdd_median:7.2f}% "
            f"PF={r.pf_median:5.2f} P(MDD<=-20)={r.prob_mdd_le_minus20_pct:5.1f}% "
            f"skipRisk~{r.skipped_risk_cap_median:5.0f}"
        )

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "snapshot_version": snap["version"],
        "snapshot_created_at_utc": snap.get("created_at_utc"),
        "method": "risk sizing stress with randomized slot allocation",
        "n_sims_per_combination_window": N_SIMS,
        "base_seed": BASE_SEED,
        "risk_per_trade_pct": [x*100 for x in RISK_PER_TRADE],
        "portfolio_risk_cap_pct": [x*100 for x in PORTFOLIO_RISK_CAP],
        "max_positions": MAX_POSITIONS,
        "frozen_strategy": pert.BASE,
        "execution": "T+1 open; TP .75 ATR; SL 5%; T7; gap-aware stop; stop-first ambiguity; 10bps RT",
        "portfolio_cap_definition": "sum of initial EUR risk budgets of open positions; no partial sizing",
        "interpretation": "Do not select by maximum return alone. Compare drawdown and tail-risk as sizing increases.",
        "summary": sdf.to_dict("records"),
    }
    JSON_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print("\nSaved risk sizing stress outputs.")


if __name__ == "__main__":
    main()
