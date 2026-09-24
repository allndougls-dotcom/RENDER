"""
SIDI experimental backtest runner.

Purpose
-------
Test improvements without modifying the production/backtest.py engine.
The runner reuses SIDI's existing signal construction but replaces portfolio
execution with a stricter simulator:

* Signal at close of day T -> entry at OPEN of next trading day T+1.
* TP/SL are active on the entry day itself.
* If TP and SL are both touched in the same daily candle, SL wins
  (conservative OHLC ambiguity rule).
* Risk per position = 1.5% of current realised capital.
* Daily mark-to-market equity curve for Max Drawdown.
* Optional ranking when more signals arrive than available slots.
* Target sweeps: fixed percentage and ATR multiples.
* Stop sweeps: fixed 5% vs ATR-based stops.
* Confirmation and time-stop experiments.

IMPORTANT LIMITATION
--------------------
Fundamental score is still the current score applied retrospectively because
that is what backtest.py currently exposes. Results involving fund_score are
therefore NOT fully point-in-time and must be treated as provisional.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import backtest as bt


START_DATE = "2024-08-11"
END_DATE = "2026-09-10"
INITIAL_CAPITAL = 10_000.0
MAX_POSITIONS = 5
RISK_PCT = 0.015


@dataclass(frozen=True)
class Experiment:
    name: str
    signal_key: str = "BASE"
    target_mode: str = "atr"       # atr | fixed
    atr_mult: float = 1.5
    target_pct: float = 0.065
    stop_mode: str = "fixed"       # fixed | atr
    stop_pct: float = 0.05
    stop_atr_mult: float = 1.5
    time_stop: int = 15
    rank_signals: bool = False
    description: str = ""


def signal_variant(dd: float = 12.0, confirm: bool = False) -> dict:
    """Signal definition used for all experiments unless explicitly changed."""
    return {
        "nombre": f"EXP_DD{dd:g}_{'CONF' if confirm else 'RAW'}",
        "rsi": 40,
        "dd": float(dd),
        "target_atr": True,
        "atr_mult": 1.5,
        "stop_pct": 0.05,
        "max_hold": 15,
        "confirmacion": bool(confirm),
        "time_stop": 15,
        "spy_filter": False,
        "min_fund_score": 6.5,
        "desc": "SCORE65 experimental signal base",
    }


def build_experiments() -> list[Experiment]:
    exps: list[Experiment] = [
        Experiment(
            "BASE_SCORE65_ATR_1_5",
            description="DD>=12, fund_score>=6.5, TP=1.5xATR, SL=5%, 15d",
        )
    ]

    # Fixed target sweep: answers the 2% vs 3% vs 5% vs 10% question directly.
    for pct, label in [
        (0.02, "2"), (0.03, "3"), (0.04, "4"), (0.05, "5"),
        (0.065, "6_5"), (0.08, "8"), (0.10, "10"), (0.12, "12"),
    ]:
        exps.append(Experiment(
            f"TP_{label}", target_mode="fixed", target_pct=pct,
            description=f"Fixed target {pct*100:g}%",
        ))

    # ATR target sweep.
    for mult, label in [
        (0.75, "0_75"), (1.0, "1"), (1.25, "1_25"), (1.5, "1_5"),
        (1.75, "1_75"), (2.0, "2"), (2.5, "2_5"),
    ]:
        exps.append(Experiment(
            f"ATR_{label}X", target_mode="atr", atr_mult=mult,
            description=f"Dynamic target {mult:g}xATR",
        ))

    # Entry quality experiments.
    exps.extend([
        Experiment(
            "CONFIRM_PREV_HIGH", signal_key="CONFIRM", atr_mult=1.5,
            description="Require close > previous-day high before setup qualifies",
        ),
        Experiment(
            "RANK_QUALITY", rank_signals=True, atr_mult=1.5,
            description="When slots are scarce, rank by fundamental+DD+RSI quality",
        ),
        Experiment(
            "CONFIRM_AND_RANK", signal_key="CONFIRM", rank_signals=True, atr_mult=1.5,
            description="Confirmation plus quality ranking",
        ),
    ])

    # Time-stop sweep, holding target/stop constant.
    for days in [5, 7, 10]:
        exps.append(Experiment(
            f"TIME_STOP_{days}", time_stop=days,
            description=f"Exit at close after {days} trading days if TP/SL not hit",
        ))

    # ATR-based stop sweep. Target remains 1.5xATR.
    for mult, label in [(1.0, "1"), (1.5, "1_5"), (2.0, "2")]:
        exps.append(Experiment(
            f"STOP_ATR_{label}X", stop_mode="atr", stop_atr_mult=mult,
            description=f"Stop distance {mult:g}xATR instead of fixed 5%",
        ))

    # Test whether the anomaly filter improves further beyond DD=12%.
    for dd, key in [(15.0, "DD15"), (18.0, "DD18"), (20.0, "DD20")]:
        exps.append(Experiment(
            f"{key}_ATR_1_5", signal_key=key,
            description=f"Minimum 60d drawdown {dd:g}% with TP=1.5xATR",
        ))

    return exps


def price_indexes(prices: dict[str, pd.DataFrame]):
    date_rows: dict[str, dict[str, pd.Series]] = {}
    for ticker, df in prices.items():
        rows = {}
        for _, row in df.iterrows():
            rows[str(row["Date"])[:10]] = row
        date_rows[ticker] = rows
    return date_rows


def schedule_entries(signal_map: dict, indicators: dict) -> dict[str, list[dict]]:
    """Move every close-of-T signal to the next ticker trading day (T+1)."""
    scheduled: dict[str, list[dict]] = {}
    for ticker, sigs in signal_map.items():
        ind = indicators[ticker]
        for signal_date, sig in sigs.items():
            i = int(sig["row_idx"])
            if i + 1 >= len(ind["dates"]):
                continue
            entry_date = ind["dates"][i + 1]
            exact_atr = float(ind["atr"][i]) if not np.isnan(ind["atr"][i]) else 0.0
            item = {
                **sig,
                "ticker": ticker,
                "signal_date": signal_date,
                "entry_date": entry_date,
                "atr_exact": exact_atr,
            }
            scheduled.setdefault(entry_date, []).append(item)
    return scheduled


def quality_score(sig: dict, fund_scores: dict) -> float:
    """
    Transparent ranking score, used ONLY in ranking experiments.
    It deliberately avoids fitted coefficients.
    """
    f = float(fund_scores.get(sig["ticker"], {}).get("fund_score", 0.0))
    dd = abs(float(sig.get("dd", 0.0)))
    rsi = float(sig.get("rsi", 40.0))
    # Fundamental quality is primary; anomaly severity and oversold condition add tie-break edge.
    return 2.0 * f + 0.10 * min(dd, 40.0) + 0.05 * max(0.0, 40.0 - rsi)


def target_and_stop(entry: float, atr: float, exp: Experiment) -> tuple[float, float, float, float]:
    if exp.target_mode == "atr":
        target = entry + exp.atr_mult * atr
    else:
        target = entry * (1.0 + exp.target_pct)

    if exp.stop_mode == "atr":
        stop = entry - exp.stop_atr_mult * atr
    else:
        stop = entry * (1.0 - exp.stop_pct)

    # Safety guards for pathological/missing ATR.
    if target <= entry:
        target = entry * (1.0 + exp.target_pct)
    if stop <= 0 or stop >= entry:
        stop = entry * (1.0 - exp.stop_pct)

    target_pct = (target - entry) / entry
    stop_distance_pct = (entry - stop) / entry
    return target, stop, target_pct, stop_distance_pct


def pnl_from_exit(pos: dict, exit_price: float) -> tuple[float, float]:
    pnl_pct = (exit_price - pos["entry"]) / pos["entry"]
    # 1R at the stop, regardless of whether the stop is percentage- or ATR-based.
    pnl_eur = pos["risk_eur"] * (pnl_pct / pos["stop_distance_pct"])
    return pnl_pct, pnl_eur


def evaluate_bar(pos: dict, row: pd.Series, allow_time_stop: bool = True):
    """Return (reason, exit_price) or (None, None). Conservative when both barriers touch."""
    high = float(row["High"])
    low = float(row["Low"])
    close = float(row["Close"])

    stop_hit = low <= pos["stop"]
    target_hit = high >= pos["target"]

    if stop_hit and target_hit:
        return "STOP_BOTH_TOUCHED", pos["stop"]
    if stop_hit:
        return "STOP", pos["stop"]
    if target_hit:
        return "TARGET", pos["target"]
    if allow_time_stop and pos["days_held"] >= pos["time_stop"]:
        return "TIME_STOP", close
    return None, None


def simulate(
    signal_map: dict,
    prices: dict[str, pd.DataFrame],
    indicators: dict,
    fund_scores: dict,
    exp: Experiment,
):
    rows_by_date = price_indexes(prices)
    scheduled = schedule_entries(signal_map, indicators)
    all_dates = sorted({d for rows in rows_by_date.values() for d in rows})
    all_dates = [d for d in all_dates if START_DATE <= d <= END_DATE]

    capital = INITIAL_CAPITAL
    open_pos: list[dict] = []
    trades: list[dict] = []
    equity_curve: list[dict] = []
    ambiguous_bars = 0
    same_day_exits = 0

    def close_position(pos: dict, date: str, reason: str, exit_price: float):
        nonlocal capital, ambiguous_bars, same_day_exits
        pnl_pct, pnl_eur = pnl_from_exit(pos, exit_price)
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
            "pnl_eur": round(pnl_eur, 2),
            "outcome": "WIN" if pnl_eur > 0 else "LOSS",
            "capital_after": round(capital, 2),
        })

    for date in all_dates:
        # 1) Manage positions that existed before today's open.
        survivors: list[dict] = []
        for pos in open_pos:
            row = rows_by_date.get(pos["ticker"], {}).get(date)
            if row is None:
                survivors.append(pos)
                continue
            pos["days_held"] += 1
            reason, exit_price = evaluate_bar(pos, row, allow_time_stop=True)
            if reason:
                close_position(pos, date, reason, float(exit_price))
            else:
                survivors.append(pos)
        open_pos = survivors

        # 2) Open T+1 signals at today's OPEN, AFTER exits free slots.
        open_tickers = {p["ticker"] for p in open_pos}
        candidates = [s for s in scheduled.get(date, []) if s["ticker"] not in open_tickers]
        if exp.rank_signals:
            candidates.sort(key=lambda s: quality_score(s, fund_scores), reverse=True)

        slots = MAX_POSITIONS - len(open_pos)
        for sig in candidates[:max(0, slots)]:
            ticker = sig["ticker"]
            row = rows_by_date.get(ticker, {}).get(date)
            if row is None or pd.isna(row.get("Open", np.nan)):
                continue

            entry = float(row["Open"])
            atr = float(sig.get("atr_exact", 0.0))
            target, stop, tp_pct, stop_dist_pct = target_and_stop(entry, atr, exp)
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
                "risk_eur": capital * RISK_PCT,
                "rsi": float(sig["rsi"]),
                "dd": float(sig["dd"]),
                "fund_score": fscore,
                "quality_score": quality_score(sig, fund_scores),
                "days_held": 1,
                "time_stop": exp.time_stop,
            }

            # TP/SL are active immediately on the entry day.
            reason, exit_price = evaluate_bar(pos, row, allow_time_stop=(exp.time_stop <= 1))
            if reason:
                close_position(pos, date, reason, float(exit_price))
            else:
                open_pos.append(pos)
                open_tickers.add(ticker)

        # 3) Daily mark-to-market equity, including unrealised P&L.
        mtm_equity = capital
        for pos in open_pos:
            row = rows_by_date.get(pos["ticker"], {}).get(date)
            if row is None:
                continue
            close = float(row["Close"])
            pnl_pct = (close - pos["entry"]) / pos["entry"]
            mtm_equity += pos["risk_eur"] * (pnl_pct / pos["stop_distance_pct"])
        equity_curve.append({"date": date, "equity": round(mtm_equity, 2)})

    # Close any remaining positions at the final available close.
    final_date = all_dates[-1] if all_dates else END_DATE
    for pos in list(open_pos):
        row = rows_by_date.get(pos["ticker"], {}).get(final_date)
        if row is None:
            # Last ticker-specific row in range.
            ticker_dates = [d for d in rows_by_date.get(pos["ticker"], {}) if d <= END_DATE]
            if not ticker_dates:
                continue
            d = max(ticker_dates)
            row = rows_by_date[pos["ticker"]][d]
        close_position(pos, final_date, "END_OF_PERIOD", float(row["Close"]))

    return trades, equity_curve, capital, ambiguous_bars, same_day_exits


def stats_for(exp: Experiment, trades: list[dict], equity_curve: list[dict], final_capital: float,
              signal_count: int, ambiguous_bars: int, same_day_exits: int) -> dict:
    df = pd.DataFrame(trades)
    eq = pd.DataFrame(equity_curve)

    if len(df):
        wins = int((df["pnl_eur"] > 0).sum())
        losses = int((df["pnl_eur"] <= 0).sum())
        wr = wins / len(df) * 100.0
        gross_profit = float(df.loc[df["pnl_eur"] > 0, "pnl_eur"].sum())
        gross_loss = abs(float(df.loc[df["pnl_eur"] < 0, "pnl_eur"].sum()))
        pf = gross_profit / gross_loss if gross_loss > 0 else 999.0
        avg_trade = float(df["pnl_eur"].mean())
        avg_trade_pct = float(df["pnl_pct"].mean())
        avg_days = float(df["days"].mean())
    else:
        wins = losses = 0
        wr = pf = avg_trade = avg_trade_pct = avg_days = 0.0

    if len(eq):
        s = eq["equity"].astype(float)
        mdd = float(((s / s.cummax()) - 1.0).min() * 100.0)
    else:
        mdd = 0.0

    total_return = (final_capital / INITIAL_CAPITAL - 1.0) * 100.0
    years = max((pd.Timestamp(END_DATE) - pd.Timestamp(START_DATE)).days / 365.25, 1e-9)
    cagr = ((final_capital / INITIAL_CAPITAL) ** (1.0 / years) - 1.0) * 100.0 if final_capital > 0 else -100.0

    return {
        "name": exp.name,
        "description": exp.description,
        "signal_count": int(signal_count),
        "trades": int(len(df)),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wr, 2),
        "profit_factor": round(pf, 3),
        "total_return": round(total_return, 2),
        "cagr": round(cagr, 2),
        "max_drawdown_mtm": round(mdd, 2),
        "avg_trade_eur": round(avg_trade, 2),
        "avg_trade_pct": round(avg_trade_pct, 3),
        "avg_days": round(avg_days, 2),
        "final_capital": round(final_capital, 2),
        "ambiguous_tp_sl_bars": int(ambiguous_bars),
        "same_day_exits": int(same_day_exits),
        "config": asdict(exp),
    }


def main():
    print("=" * 86)
    print("SIDI EXPERIMENTAL BACKTEST — stricter T+1 execution + target/ATR improvements")
    print("=" * 86)
    print(f"Period: {START_DATE} -> {END_DATE}")
    print(f"Capital: EUR {INITIAL_CAPITAL:,.0f} | risk/trade: {RISK_PCT*100:.1f}% | max positions: {MAX_POSITIONS}")
    print("Conservative rule: if TP and SL touch in same daily candle -> STOP")
    print("WARNING: fundamental score remains a current-score historical proxy.\n")

    tickers = bt.load_tickers()
    fund_scores = bt.load_fundamental_scores()
    eligible = sum(1 for x in fund_scores.values() if x.get("fund_score", 0) >= 6.5)
    print(f"fund_score >= 6.5: {eligible}/{len(fund_scores)}")

    prices = bt.download_prices(tickers, start_date=START_DATE, end_date=END_DATE)
    print("Building indicators...")
    indicators = bt.build_indicators(prices)

    signal_specs = {
        "BASE": signal_variant(12.0, False),
        "CONFIRM": signal_variant(12.0, True),
        "DD15": signal_variant(15.0, False),
        "DD18": signal_variant(18.0, False),
        "DD20": signal_variant(20.0, False),
    }

    signal_maps = {}
    signal_counts = {}
    for key, spec in signal_specs.items():
        smap, n = bt.build_signal_map(
            indicators, spec, spy_dict={}, date_from=START_DATE, date_to=END_DATE,
            fund_scores=fund_scores, sector_regime=None,
        )
        signal_maps[key] = smap
        signal_counts[key] = n
        print(f"Signals {key:<7}: {n}")

    results = []
    detailed = {}
    experiments = build_experiments()
    print(f"\nRunning {len(experiments)} experiments...\n")

    for i, exp in enumerate(experiments, 1):
        trades, equity, final_cap, ambiguous, same_day = simulate(
            signal_maps[exp.signal_key], prices, indicators, fund_scores, exp
        )
        st = stats_for(
            exp, trades, equity, final_cap,
            signal_counts[exp.signal_key], ambiguous, same_day,
        )
        results.append(st)
        detailed[exp.name] = {"summary": st, "trades": trades, "equity": equity}
        print(
            f"[{i:02d}/{len(experiments)}] {exp.name:<22} "
            f"WR={st['win_rate']:>6.2f}%  PF={st['profit_factor']:>5.2f}  "
            f"Ret={st['total_return']:>8.2f}%  CAGR={st['cagr']:>6.2f}%  "
            f"MDD={st['max_drawdown_mtm']:>7.2f}%  Days={st['avg_days']:>5.2f}  "
            f"Trades={st['trades']}"
        )

    summary_df = pd.DataFrame(results)
    # Keep config dict out of CSV; JSON retains it.
    csv_df = summary_df.drop(columns=["config"], errors="ignore").sort_values(
        ["total_return", "profit_factor"], ascending=False
    )
    csv_df.to_csv("sidi_experiment_summary.csv", index=False)

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "period": {"start": START_DATE, "end": END_DATE},
        "assumptions": {
            "initial_capital": INITIAL_CAPITAL,
            "risk_pct": RISK_PCT,
            "max_positions": MAX_POSITIONS,
            "entry": "next trading day OPEN after close-of-day signal",
            "same_day_barriers": True,
            "both_tp_sl_same_candle": "STOP (conservative)",
            "mdd": "daily mark-to-market equity",
            "fundamental_score": "current-score proxy applied historically; NOT point-in-time",
        },
        "results": results,
        "details": detailed,
    }
    Path("sidi_experiment_results.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("\n" + "=" * 86)
    print("TOP 10 BY TOTAL RETURN")
    print("=" * 86)
    cols = ["name", "win_rate", "profit_factor", "total_return", "cagr", "max_drawdown_mtm", "avg_days", "trades"]
    print(csv_df[cols].head(10).to_string(index=False))
    print("\nSaved: sidi_experiment_summary.csv + sidi_experiment_results.json")


if __name__ == "__main__":
    main()
