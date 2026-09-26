"""Execution stress test for the frozen complete-case SIDI candidate.

No parameter optimisation. The signal definition is identical to the current
complete-case reference. This script only makes execution progressively more
realistic/adverse:

- gap through stop exits at the actual next OPEN, not the stop price;
- conservative STOP-first handling when TP and SL touch in the same daily bar;
- adverse entry/market-exit slippage;
- round-trip implementation costs on actual notional.

Target orders are treated as resting limit orders and therefore fill at the
specified target, never worse. Stop and time/end exits are marketable orders and
can suffer adverse slippage.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import backtest_complete_case_reference as ref
import backtest_context_filters_pit_oos as base
import backtest as bt
import backtest_experiments as exp
import validate_sidi_candidate as val
import historical_membership as membership
import calibrate_pit_proxy_ttm as cal_ttm
import pit_proxy_ttm_fast as ttm_fast
import analyze_signal_context_features as feat
import backtest_context_filters as ctxmod


@dataclass(frozen=True)
class ExecutionScenario:
    name: str
    gap_aware_stop: bool
    cost_bps_rt: float
    slippage_bps: float
    description: str


SCENARIOS = [
    ExecutionScenario(
        "LEGACY_10BPS", False, 10, 0,
        "Current reference engine: stop fills at stop even after an overnight gap",
    ),
    ExecutionScenario(
        "GAP_REAL_10BPS", True, 10, 0,
        "Gap-aware stop, same 10 bps round-trip implementation cost",
    ),
    ExecutionScenario(
        "MODERATE", True, 20, 5,
        "Gap-aware + 20 bps RT cost + 5 bps adverse entry/market-exit slippage",
    ),
    ExecutionScenario(
        "HARSH", True, 30, 10,
        "Gap-aware + 30 bps RT cost + 10 bps adverse entry/market-exit slippage",
    ),
    ExecutionScenario(
        "VERY_HARSH", True, 50, 15,
        "Gap-aware + 50 bps RT cost + 15 bps adverse entry/market-exit slippage",
    ),
]

WINDOWS = [
    ("FULL_2023_2026", ref.START, ref.END),
    ("OOS_2023_TO_2024_08", "2023-01-01", "2024-08-10"),
    ("DEV_2024_08_TO_2026", "2024-08-11", ref.END),
    ("RECENT_2025_09_TO_2026", "2025-09-01", ref.END),
]


def build_complete_case_inputs():
    """Rebuild exactly the frozen complete-case candidate map."""
    master, sectors = feat.load_master()
    tickers = bt.load_tickers()
    fund_scores = bt.load_fundamental_scores()
    added = membership.load_date_added()

    prices = bt.download_prices(tickers, start_date=ref.PRICE_START, end_date=ref.END)
    tickers = [t for t in tickers if t in prices and prices[t] is not None and not prices[t].empty]
    prices = {t: prices[t] for t in tickers}
    indicators = bt.build_indicators(prices)
    prices_idx = {t: feat.to_indexed(d) for t, d in prices.items()}
    pmap = base.price_maps(prices)

    spec = exp.signal_variant(12.0, False)
    spec["min_fund_score"] = 0.0
    raw, nraw = bt.build_signal_map(
        indicators, spec, spy_dict={}, date_from=ref.START, date_to=ref.END,
        fund_scores=fund_scores, sector_regime=None,
    )
    tech_map, member_stats = membership.filter_signal_map(raw, added)
    signal_dates = sorted({d for sigs in tech_map.values() for d in sigs})

    statements, failed = cal_ttm.fetch_full_statements(tickers)
    state_cache = ttm_fast.build_state_cache(statements, ref.START, ref.END)
    scores, coverage = base.dynamic_scores(signal_dates, tickers, sectors, state_cache, pmap, added)
    pit_map, pit_stats = base.filter_pit(tech_map, scores)

    feat.DOWNLOAD_START = ref.PRICE_START
    refs = {
        s: feat.download_reference(s)
        for s in sorted(set(feat.SECTOR_ETF.values()) | {"SPY", "^VIX"})
    }
    market_breadth, sector_breadth = feat.breadth_maps(prices_idx, sectors, added)
    context = ctxmod.build_context(
        pit_map, prices_idx, sectors, refs, market_breadth, sector_breadth
    )

    def complete_candidate(r):
        return (
            base.present(r.get("spy20"))
            and base.present(r.get("abnormal20"))
            and r["spy20"] <= 1.0
            and r["abnormal20"] <= -10.0
        )

    candidate_map, candidate_signals = ctxmod.subset_signal_map(
        pit_map, context, complete_candidate
    )
    meta = {
        "downloaded_price_tickers": len(tickers),
        "technical_raw": nraw,
        "membership": member_stats,
        "pit_filter": pit_stats,
        "context_complete_signals": candidate_signals,
        "statement_failures": failed,
    }
    return prices, indicators, fund_scores, candidate_map, coverage, meta


def simulate_execution(signal_map, prices, indicators, fund_scores, scenario, start, end):
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
    slip = float(scenario.slippage_bps) / 10_000.0
    cost_rate = float(scenario.cost_bps_rt) / 10_000.0

    def pnl_net(pos, exit_price):
        pnl_pct = (exit_price - pos["entry"]) / pos["entry"]
        gross = pos["risk_eur"] * (pnl_pct / pos["stop_distance_pct"])
        notional = pos["risk_eur"] / pos["stop_distance_pct"]
        cost = notional * cost_rate
        return pnl_pct, gross - cost, gross, cost

    def close_position(pos, date, reason, exit_price):
        nonlocal capital, ambiguous_bars, same_day_exits
        pnl_pct, pnl_eur, gross_pnl, implementation_cost = pnl_net(pos, exit_price)
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
            "quality_score": round(pos["quality_score"], 3),
            "days": pos["days_held"],
            "exit_reason": reason,
            "pnl_pct": round(pnl_pct * 100, 4),
            "gross_pnl_eur": round(gross_pnl, 2),
            "implementation_cost_eur": round(implementation_cost, 2),
            "pnl_eur": round(pnl_eur, 2),
            "outcome": "WIN" if pnl_eur > 0 else "LOSS",
            "capital_after": round(capital, 2),
        })

    def evaluate(pos, row, allow_time_stop=True, existing_position=True):
        open_px = float(row["Open"])
        high = float(row["High"])
        low = float(row["Low"])
        close = float(row["Close"])

        # Only positions carried overnight can gap through a pre-existing stop.
        if scenario.gap_aware_stop and existing_position and open_px <= pos["stop"]:
            fill = open_px * (1.0 - slip)
            beyond = (open_px / pos["stop"] - 1.0) * 100.0
            theoretical_stop_pnl, _, _, _ = pnl_net(pos, pos["stop"])
            actual_gap_pnl, _, _, _ = pnl_net(pos, fill)
            gap_events.append({
                "ticker": pos["ticker"], "date": str(row["Date"])[:10],
                "stop": pos["stop"], "open": open_px, "fill": fill,
                "gap_beyond_stop_pct": beyond,
                "pnl_pct_at_stop": theoretical_stop_pnl * 100.0,
                "pnl_pct_actual": actual_gap_pnl * 100.0,
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
            # Resting sell limit: conservative fill at the limit itself.
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
        if ref.base.CANDIDATE.rank_signals:
            candidates.sort(key=lambda s: exp.quality_score(s, fund_scores), reverse=True)

        slots = exp.MAX_POSITIONS - len(open_pos)
        for sig in candidates[:max(0, slots)]:
            ticker = sig["ticker"]
            row = rows_by_date.get(ticker, {}).get(date)
            if row is None or pd.isna(row.get("Open", np.nan)):
                continue

            raw_open = float(row["Open"])
            entry = raw_open * (1.0 + slip)
            atr = float(sig.get("atr_exact", 0.0))
            target, stop, tp_pct, stop_dist_pct = exp.target_and_stop(
                entry, atr, base.CANDIDATE
            )
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
                "quality_score": exp.quality_score(sig, fund_scores),
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

    return trades, equity_curve, capital, ambiguous_bars, same_day_exits, gap_events


def run_scenario(signal_map, prices, indicators, fund_scores, scenario, start, end):
    old_start, old_end = exp.START_DATE, exp.END_DATE
    try:
        exp.START_DATE, exp.END_DATE = start, end
        if scenario.name == "LEGACY_10BPS":
            st, trades = val.run_sim(
                signal_map, prices, indicators, fund_scores, base.CANDIDATE,
                start, end, cost_bps=scenario.cost_bps_rt,
            )
            st.update({
                "gap_stop_count": 0,
                "avg_gap_beyond_stop_pct": 0.0,
                "worst_gap_beyond_stop_pct": 0.0,
                "slippage_bps_each_market_side": 0.0,
                "execution_model": scenario.name,
            })
            return st, trades, []

        trades, equity, final_cap, ambiguous, same_day, gaps = simulate_execution(
            signal_map, prices, indicators, fund_scores, scenario, start, end
        )
        st = exp.stats_for(
            base.CANDIDATE, trades, equity, final_cap,
            signal_count=sum(len(v) for v in signal_map.values()),
            ambiguous_bars=ambiguous, same_day_exits=same_day,
        )
        gap_vals = [g["gap_beyond_stop_pct"] for g in gaps]
        st.update({
            "validation_start": start,
            "validation_end": end,
            "round_trip_cost_bps": scenario.cost_bps_rt,
            "gap_stop_count": len(gaps),
            "avg_gap_beyond_stop_pct": round(float(np.mean(gap_vals)), 4) if gap_vals else 0.0,
            "worst_gap_beyond_stop_pct": round(float(np.min(gap_vals)), 4) if gap_vals else 0.0,
            "slippage_bps_each_market_side": scenario.slippage_bps,
            "execution_model": scenario.name,
        })
        return st, trades, gaps
    finally:
        exp.START_DATE, exp.END_DATE = old_start, old_end


def main():
    print("=" * 118)
    print("SIDI EXECUTION STRESS — COMPLETE-CASE CONTEXT CANDIDATE")
    print("=" * 118)
    print("Frozen signal/portfolio parameters. Only execution assumptions change.\n")

    prices, indicators, fund_scores, candidate_map, coverage, meta = build_complete_case_inputs()

    rows = []
    all_gaps = []
    detailed = {}
    for window, start, end in WINDOWS:
        print(f"\n{window}: {start} -> {end}")
        for scenario in SCENARIOS:
            st, trades, gaps = run_scenario(
                candidate_map, prices, indicators, fund_scores, scenario, start, end
            )
            st.update({"window": window, "scenario": scenario.name})
            rows.append(st)
            for g in gaps:
                all_gaps.append({"window": window, "scenario": scenario.name, **g})
            detailed[f"{window}|{scenario.name}"] = {
                "summary": st,
                "trades": trades,
            }
            print(
                f"  {scenario.name:<18} trades={st['trades']:4d} WR={st['win_rate']:6.2f}% "
                f"PF={st['profit_factor']:5.2f} Ret={st['total_return']:8.2f}% "
                f"MDD={st['max_drawdown_mtm']:7.2f}% gaps={st['gap_stop_count']:3d} "
                f"worstGap={st['worst_gap_beyond_stop_pct']:6.2f}%"
            )

    summary = pd.DataFrame(rows).drop(columns=["config"], errors="ignore")
    summary.to_csv("sidi_execution_stress.csv", index=False)
    pd.DataFrame(all_gaps).to_csv("sidi_execution_gap_events.csv", index=False)
    coverage.to_csv("sidi_execution_pit_coverage.csv", index=False)

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "candidate": {
            "pit_min": 6.66, "min_coverage": 6,
            "spy20_max": 1.0, "abnormal20_max": -10.0,
            "entry": "T+1 open", "tp": "0.75xATR14", "sl_pct": 5.0,
            "time_stop": 7, "risk_pct": 1.5, "max_positions": 5,
        },
        "scenarios": [asdict(s) for s in SCENARIOS],
        "metadata": meta,
        "results": rows,
        "gap_events": all_gaps,
        "details": detailed,
    }
    Path("sidi_execution_stress.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print("\nSaved execution-stress outputs.")


if __name__ == "__main__":
    main()
