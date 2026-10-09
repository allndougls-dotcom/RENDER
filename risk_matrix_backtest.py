#!/usr/bin/env python3
"""
SIDI_INTRADAY_V2 · Risk Matrix Backtest

Varia solo dos variables:
- riesgo por operación
- riesgo máximo agregado de cartera

Limitaciones del proxy histórico:
- fund_score actual aplicado retroactivamente (sesgo conocido del backtest legado);
- entrada histórica en siguiente Open, porque no existe histórico 5m multianual suficiente;
- OHLC diario resuelve conflictos de forma conservadora: SL/BE primero.

El objetivo es comparar MONEY MANAGEMENT, no reestimar el edge de la estrategia.
"""
from __future__ import annotations
import argparse, json, math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
import numpy as np
import pandas as pd
import yfinance as yf

from backtest import INITIAL_CAP, build_indicators, download_prices, load_fundamental_scores, load_tickers
from modules.ingesta.sidi_context import SECTOR_ETF_MAP, _abnormal20, _close_series, _spy20

STOP_PCT = 0.05
TP1_ATR_MULT = 1.0
TP2_ATR_MULT = 1.5
TIME_STOP_SESSIONS = 7
MAX_POSITIONS = 3
COST_PCT_RT = 0.001

TRADE_RISKS = [0.50, 0.75, 1.00, 1.25, 1.50, 1.75, 2.00]
PORTFOLIO_CAPS = [round(x * 0.25, 2) for x in range(2, 25)]  # 0.50% .. 6.00%
STRATEGY_VERSION = "SIDI_INTRADAY_V2"
MODEL_VERSION = "RISK_MATRIX_DAILY_PROXY_V2"  # exact SPY+sector abnormal20 context


@dataclass
class Position:
    ticker: str
    signal_date: str
    entry_date: str
    entry_price: float
    atr: float
    stop: float
    tp1: float
    tp2: float
    shares_initial: float
    shares_remaining: float
    risk_eur: float
    entry_notional_eur: float
    cost_eur: float
    fund_score: float
    dd60: float
    rsi: float
    spy20: float
    abnormal20: float
    sessions: int = 0
    tp1_hit: bool = False
    tp1_date: str = ""
    realized_proceeds_eur: float = 0.0
    exit_reason: str = ""
    exit_date: str = ""
    last_price: float = 0.0


def norm_ticker(ticker: str) -> str:
    return str(ticker).strip().upper().replace(".", "-")


def download_context_benchmarks(years: int):
    """Descarga SPY + ETFs sectoriales para reproducir el contexto de producción."""
    tickers = ["SPY"] + sorted(set(SECTOR_ETF_MAP.values()))
    end = pd.Timestamp.today().normalize()
    start = end - pd.Timedelta(days=365 * years + 320)
    raw = yf.download(
        tickers,
        start=start.strftime("%Y-%m-%d"),
        end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
        auto_adjust=True, progress=False, group_by="ticker", threads=True,
    )
    out = {}
    for ticker in tickers:
        try:
            df_t = raw[ticker].copy() if isinstance(raw.columns, pd.MultiIndex) else raw.copy()
            s = _close_series(df_t)
            if len(s) >= 141:
                out[ticker] = s
        except Exception:
            continue
    if "SPY" not in out:
        raise RuntimeError("No se pudo descargar SPY para el contexto SIDI")
    return out


def build_signals(prices, indicators, fund_scores, benchmarks):
    """
    Proxy histórico del contrato congelado:
    DD60<=-12 · RSI<40 · MACD improving · volume decreasing ·
    fund_score>=6.5 · SPY20<=+1% · abnormal20<=-10%.

    abnormal20 replica modules.ingesta.sidi_context:
    alpha + beta_SPY + beta_sector estimados en 120 sesiones previas y
    residual acumulado de las 20 sesiones recientes.
    Entrada histórica: siguiente Open.
    """
    entries_by_date = {}
    counts = {
        "technical_candidates": 0, "fund_tickers": 0, "context_ready": 0,
        "full_setups": 0, "skipped_no_next_open": 0, "missing_sector_context": 0,
    }
    spy_close_all = benchmarks["SPY"]

    for ticker, ind in indicators.items():
        fs = fund_scores.get(ticker) or fund_scores.get(ticker.replace("-", "."))
        if not fs or float(fs.get("fund_score", 0.0)) < 6.5:
            continue
        counts["fund_tickers"] += 1
        df = prices.get(ticker)
        if df is None or len(df) < 170:
            continue

        sector = str(fs.get("sector", ""))
        sector_etf = SECTOR_ETF_MAP.get(sector)
        sector_close_all = benchmarks.get(sector_etf) if sector_etf else None
        if sector_close_all is None or len(sector_close_all) < 141:
            counts["missing_sector_context"] += 1
            continue

        df = df.copy().reset_index(drop=True)
        stock_close_all = _close_series(df)
        date_to_idx = {str(row["Date"])[:10]: i for i, row in df.iterrows()}
        dates = ind["dates"]

        for i in range(62, len(dates) - 1):
            rsi, dd60 = ind["rsi"][i], ind["dd60"][i]
            hist, hist_prev = ind["hist"][i], ind["hist"][i - 1]
            vdec, atr = ind["vdec"][i], ind["atr"][i]
            if not (
                pd.notna(rsi) and float(rsi) < 40.0 and
                pd.notna(dd60) and float(dd60) <= -12.0 and
                pd.notna(hist) and pd.notna(hist_prev) and float(hist) > float(hist_prev) and
                bool(vdec) and pd.notna(atr) and float(atr) > 0
            ):
                continue
            counts["technical_candidates"] += 1

            signal_date = dates[i]
            cutoff = pd.Timestamp(signal_date).tz_localize(None).normalize()
            stock_cut = stock_close_all.loc[:cutoff]
            spy_cut = spy_close_all.loc[:cutoff]
            sector_cut = sector_close_all.loc[:cutoff]
            spy20 = _spy20(spy_cut)
            abnormal20, _, _, _ = _abnormal20(stock_cut, spy_cut, sector_cut)
            if not (np.isfinite(spy20) and np.isfinite(abnormal20)):
                continue
            counts["context_ready"] += 1
            if float(spy20) > 1.0 or float(abnormal20) > -10.0:
                continue

            row_idx = date_to_idx.get(signal_date)
            if row_idx is None or row_idx + 1 >= len(df):
                counts["skipped_no_next_open"] += 1
                continue
            next_row = df.iloc[row_idx + 1]
            entry_date = str(next_row["Date"])[:10]
            entry_price = float(next_row["Open"])
            if not np.isfinite(entry_price) or entry_price <= 0:
                counts["skipped_no_next_open"] += 1
                continue

            entries_by_date.setdefault(entry_date, []).append({
                "ticker": ticker, "signal_date": signal_date, "entry_date": entry_date,
                "entry_price": entry_price, "atr": float(atr),
                "fund_score": float(fs.get("fund_score", 0.0)),
                "dd60": float(dd60), "rsi": float(rsi),
                "spy20": float(spy20), "abnormal20": float(abnormal20),
                "sector": sector, "sector_etf": sector_etf,
            })
            counts["full_setups"] += 1

    for _, signals in entries_by_date.items():
        signals.sort(key=lambda s: (-s["fund_score"], s["abnormal20"], s["dd60"], s["rsi"], s["ticker"]))
    return entries_by_date, counts


def make_price_maps(prices):
    maps, all_dates = {}, set()
    for ticker, df in prices.items():
        per_date = {}
        for _, row in df.iterrows():
            d = str(row["Date"])[:10]
            try:
                per_date[d] = {k: float(row[k]) for k in ("Open", "High", "Low", "Close")}
                all_dates.add(d)
            except Exception:
                pass
        maps[ticker] = per_date
    return maps, sorted(all_dates)


def planned_open_risk_eur(positions):
    total = 0.0
    for p in positions.values():
        if p.shares_remaining <= 0:
            continue
        effective_stop = p.entry_price if p.tp1_hit else p.stop
        total += max(p.entry_price - effective_stop, 0.0) * p.shares_remaining
    return total


def mark_equity(cash, positions, price_maps, date, field="Close"):
    equity = float(cash)
    for p in positions.values():
        row = price_maps.get(p.ticker, {}).get(date)
        if row is not None:
            px = float(row[field])
            p.last_price = px
        else:
            px = p.last_price or p.entry_price
        equity += p.shares_remaining * px
    return max(equity, 0.01)


def close_position(p, price, date, reason, cash, trades):
    proceeds = p.shares_remaining * float(price)
    cash += proceeds
    p.realized_proceeds_eur += proceeds
    p.shares_remaining = 0.0
    pnl = p.realized_proceeds_eur - p.entry_notional_eur - p.cost_eur
    trades.append({
        "ticker": p.ticker, "signal_date": p.signal_date, "entry_date": p.entry_date,
        "exit_date": date, "exit_reason": reason, "entry_price": p.entry_price,
        "exit_price": float(price), "risk_eur": p.risk_eur, "pnl_eur": pnl,
        "r_multiple": pnl / p.risk_eur if p.risk_eur else np.nan,
        "cost_eur": p.cost_eur, "sessions": p.sessions, "tp1_hit": p.tp1_hit,
        "fund_score": p.fund_score, "dd60": p.dd60, "rsi": p.rsi,
        "spy20": p.spy20, "abnormal20": p.abnormal20,
    })
    return cash


def calc_metrics(eq, tr, trade_risk_pct, portfolio_cap_pct, accepted, rejected,
                 max_positions_seen, max_planned_risk_pct, avg_gross_exposure_pct,
                 gap_exits, worst_gap_r):
    if eq.empty:
        return {}
    s = pd.Series(eq["equity"].values, index=pd.to_datetime(eq["date"]))
    daily = s.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    dd = s / s.cummax() - 1.0
    mdd = float(dd.min() * 100.0)
    years = max((s.index[-1] - s.index[0]).days / 365.25, 1 / 365.25)
    total_return = float((s.iloc[-1] / s.iloc[0] - 1.0) * 100.0)
    cagr = float(((s.iloc[-1] / s.iloc[0]) ** (1.0 / years) - 1.0) * 100.0)
    std = daily.std(ddof=0) if len(daily) else 0.0
    ann_vol = float(std * np.sqrt(252) * 100.0) if std > 0 else 0.0
    sharpe = float(daily.mean() / std * np.sqrt(252)) if std > 0 else 0.0
    downside = daily[daily < 0]
    dstd = downside.std(ddof=0) if len(downside) else 0.0
    sortino = float(daily.mean() / dstd * np.sqrt(252)) if dstd > 0 else 0.0
    calmar = float(cagr / abs(mdd)) if mdd < 0 else 999.0

    worst_day = float(daily.min() * 100.0) if len(daily) else 0.0
    weekly = s.resample("W-FRI").last().pct_change().dropna()
    monthly = s.resample("ME").last().pct_change().dropna()
    worst_week = float(weekly.min() * 100.0) if len(weekly) else 0.0
    worst_month = float(monthly.min() * 100.0) if len(monthly) else 0.0

    if tr.empty:
        wins = losses = 0
        win_rate = pf = avg_r = total_cost = 0.0
    else:
        wins = int((tr["pnl_eur"] > 0).sum())
        losses = int((tr["pnl_eur"] <= 0).sum())
        win_rate = wins / len(tr) * 100.0
        gp = float(tr.loc[tr["pnl_eur"] > 0, "pnl_eur"].sum())
        gl = float(-tr.loc[tr["pnl_eur"] < 0, "pnl_eur"].sum())
        pf = gp / gl if gl > 0 else 999.0
        avg_r = float(tr["r_multiple"].mean())
        total_cost = float(tr["cost_eur"].sum()) if "cost_eur" in tr.columns else 0.0

    signals_seen = accepted + sum(int(v) for v in rejected.values())
    return {
        "trade_risk_pct": trade_risk_pct,
        "portfolio_risk_cap_pct": portfolio_cap_pct,
        "max_positions": MAX_POSITIONS,
        "initial_capital": INITIAL_CAP,
        "final_equity": round(float(s.iloc[-1]), 2),
        "total_return_pct": round(total_return, 3),
        "cagr_pct": round(cagr, 3),
        "max_drawdown_pct": round(mdd, 3),
        "calmar": round(calmar, 4),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "ann_vol_pct": round(ann_vol, 3),
        "worst_day_pct": round(worst_day, 3),
        "worst_week_pct": round(worst_week, 3),
        "worst_month_pct": round(worst_month, 3),
        "trades": int(len(tr)),
        "wins": wins, "losses": losses,
        "win_rate_pct": round(win_rate, 3),
        "profit_factor": round(pf, 4),
        "avg_r_multiple": round(avg_r, 4),
        "accepted_entries": accepted,
        "signals_seen": signals_seen,
        "rejected_risk_cap": int(rejected["risk_cap"]),
        "rejected_slots": int(rejected["slots"]),
        "rejected_cash": int(rejected["cash"]),
        "rejected_already_open": int(rejected["already_open"]),
        "acceptance_rate_pct": round(accepted / signals_seen * 100.0, 3) if signals_seen else 0.0,
        "max_positions_seen": max_positions_seen,
        "max_planned_risk_pct": round(max_planned_risk_pct, 3),
        "avg_planned_risk_pct": round(float(eq["planned_risk_pct"].mean()), 3),
        "avg_gross_exposure_pct": round(avg_gross_exposure_pct, 3),
        "max_gross_exposure_pct": round(float(eq["gross_exposure_pct"].max()), 3),
        "gap_exits": gap_exits,
        "worst_gap_r": round(worst_gap_r, 4),
        "embedded_cost_model_rt_pct": COST_PCT_RT * 100.0,
        "total_cost_eur": round(total_cost, 2),
    }


def simulate_configuration(entries_by_date, price_maps, all_dates, trade_risk_pct, portfolio_cap_pct):
    cash = float(INITIAL_CAP)
    positions = {}
    trades = []
    equity_rows = []
    rejected = {"risk_cap": 0, "slots": 0, "cash": 0, "already_open": 0}
    accepted = 0
    max_positions_seen = 0
    max_planned_risk_pct = 0.0
    gross_exposure_pcts = []
    gap_exits = 0
    worst_gap_r = 0.0

    for date in all_dates:
        # 1. Gaps de posiciones ya abiertas.
        for ticker in list(positions):
            p = positions[ticker]
            row = price_maps.get(ticker, {}).get(date)
            if row is None or date == p.entry_date:
                continue
            p.last_price = row["Open"]
            stop_now = p.entry_price if p.tp1_hit else p.stop
            if row["Open"] <= stop_now:
                before = len(trades)
                cash = close_position(p, row["Open"], date, "GAP_BE" if p.tp1_hit else "GAP_SL", cash, trades)
                if len(trades) > before:
                    gap_exits += 1
                    worst_gap_r = min(worst_gap_r, float(trades[-1]["r_multiple"]))
                del positions[ticker]

        # 2. Nuevas entradas al Open.
        for sig in entries_by_date.get(date, []):
            ticker = sig["ticker"]
            if ticker in positions:
                rejected["already_open"] += 1
                continue
            if len(positions) >= MAX_POSITIONS:
                rejected["slots"] += 1
                continue

            equity_open = mark_equity(cash, positions, price_maps, date, "Open")
            target_risk = equity_open * trade_risk_pct / 100.0
            cap_eur = equity_open * portfolio_cap_pct / 100.0
            current_risk = planned_open_risk_eur(positions)

            entry = float(sig["entry_price"])
            stop = entry * (1.0 - STOP_PCT)
            per_share_risk = entry - stop
            shares = math.floor(target_risk / per_share_risk) if per_share_risk > 0 else 0
            if shares < 1:
                rejected["cash"] += 1
                continue
            actual_risk = shares * per_share_risk
            if current_risk + actual_risk > cap_eur + 1e-9:
                rejected["risk_cap"] += 1
                continue

            notional = shares * entry
            cost = notional * COST_PCT_RT
            if notional + cost > cash + 1e-9:
                # No se escala: el riesgo/trade debe mantenerse fijo.
                rejected["cash"] += 1
                continue

            cash -= notional + cost
            positions[ticker] = Position(
                ticker=ticker, signal_date=sig["signal_date"], entry_date=date,
                entry_price=entry, atr=float(sig["atr"]), stop=stop,
                tp1=entry + TP1_ATR_MULT * float(sig["atr"]),
                tp2=entry + TP2_ATR_MULT * float(sig["atr"]),
                shares_initial=float(shares), shares_remaining=float(shares),
                risk_eur=actual_risk, entry_notional_eur=notional, cost_eur=cost,
                fund_score=float(sig["fund_score"]), dd60=float(sig["dd60"]),
                rsi=float(sig["rsi"]), spy20=float(sig["spy20"]),
                abnormal20=float(sig["abnormal20"]), last_price=entry,
            )
            accepted += 1

        # 3. Resolución intradía diaria conservadora.
        for ticker in list(positions):
            p = positions[ticker]
            row = price_maps.get(ticker, {}).get(date)
            if row is None:
                continue
            p.sessions += 1
            p.last_price = row["Close"]
            stop_now = p.entry_price if p.tp1_hit else p.stop

            if row["Low"] <= stop_now:
                cash = close_position(p, stop_now, date, "BE" if p.tp1_hit else "SL", cash, trades)
                del positions[ticker]
                continue

            if (not p.tp1_hit) and row["High"] >= p.tp1:
                half = min(p.shares_initial * 0.5, p.shares_remaining)
                cash += half * p.tp1
                p.realized_proceeds_eur += half * p.tp1
                p.shares_remaining -= half
                p.tp1_hit = True
                p.tp1_date = date
                if row["Low"] <= p.entry_price and p.shares_remaining > 0:
                    cash = close_position(p, p.entry_price, date, "TP1_BE", cash, trades)
                    del positions[ticker]
                    continue

            if ticker in positions and p.tp1_hit and row["High"] >= p.tp2:
                cash = close_position(p, p.tp2, date, "TP2", cash, trades)
                del positions[ticker]
                continue

            if ticker in positions and p.sessions >= TIME_STOP_SESSIONS:
                cash = close_position(p, row["Close"], date, "T7", cash, trades)
                del positions[ticker]

        # 4. Equity mark-to-market y telemetría de riesgo.
        equity_close = mark_equity(cash, positions, price_maps, date, "Close")
        risk_eur = planned_open_risk_eur(positions)
        risk_pct = risk_eur / equity_close * 100.0 if equity_close else 0.0
        gross = sum(
            p.shares_remaining * price_maps.get(p.ticker, {}).get(date, {}).get("Close", p.last_price or p.entry_price)
            for p in positions.values()
        )
        gross_pct = gross / equity_close * 100.0 if equity_close else 0.0
        max_positions_seen = max(max_positions_seen, len(positions))
        max_planned_risk_pct = max(max_planned_risk_pct, risk_pct)
        gross_exposure_pcts.append(gross_pct)
        equity_rows.append({
            "date": date, "equity": equity_close, "cash": cash,
            "open_positions": len(positions), "planned_risk_pct": risk_pct,
            "gross_exposure_pct": gross_pct,
        })

    if all_dates:
        final_date = all_dates[-1]
        for ticker in list(positions):
            p = positions[ticker]
            row = price_maps.get(ticker, {}).get(final_date)
            price = row["Close"] if row else (p.last_price or p.entry_price)
            cash = close_position(p, price, final_date, "END_OF_PERIOD", cash, trades)
            del positions[ticker]
        if equity_rows:
            equity_rows[-1].update({
                "equity": cash, "cash": cash, "open_positions": 0,
                "planned_risk_pct": 0.0, "gross_exposure_pct": 0.0,
            })

    eq = pd.DataFrame(equity_rows)
    tr = pd.DataFrame(trades)
    summary = calc_metrics(
        eq, tr, trade_risk_pct, portfolio_cap_pct, accepted, rejected,
        max_positions_seen, max_planned_risk_pct,
        float(np.mean(gross_exposure_pcts)) if gross_exposure_pcts else 0.0,
        gap_exits, worst_gap_r,
    )
    return summary, trades, eq


def pareto_front(df):
    if df.empty:
        return df
    keep = []
    for i, row in df.iterrows():
        ret, risk = float(row["cagr_pct"]), abs(float(row["max_drawdown_pct"]))
        dominated = False
        for j, other in df.iterrows():
            if i == j:
                continue
            oret, orisk = float(other["cagr_pct"]), abs(float(other["max_drawdown_pct"]))
            if (oret >= ret and orisk <= risk) and (oret > ret or orisk < risk):
                dominated = True
                break
        if not dominated:
            keep.append(i)
    return df.loc[keep].sort_values(["max_drawdown_pct", "cagr_pct"], ascending=[False, False])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=int, default=4)
    p.add_argument("--quick", action="store_true", help="Smoke test con 80 tickers")
    p.add_argument("--out-dir", default="data/risk_matrix")
    args = p.parse_args()

    print("=" * 78)
    print("SIDI_INTRADAY_V2 · RISK MATRIX")
    print(f"Modelo: {MODEL_VERSION} · Capital €{INITIAL_CAP:,.0f} · max posiciones {MAX_POSITIONS}")
    print("=" * 78)

    all_tickers = [norm_ticker(t) for t in load_tickers()]
    fund_scores = {norm_ticker(t): v for t, v in load_fundamental_scores().items()}
    tickers = [
        t for t in all_tickers
        if t in fund_scores and float(fund_scores[t].get("fund_score", 0.0)) >= 6.5
    ]
    if args.quick:
        tickers = tickers[:80]
    print(f"Universo tras gate fundamental >=6.5: {len(tickers)}/{len(all_tickers)}")
    prices = {norm_ticker(t): df for t, df in download_prices(tickers, years=args.years).items()}
    if not prices:
        raise RuntimeError("No hay precios")

    indicators = build_indicators(prices)
    benchmarks = download_context_benchmarks(args.years)
    entries_by_date, signal_counts = build_signals(prices, indicators, fund_scores, benchmarks)
    price_maps, all_dates = make_price_maps(prices)
    print("Signal counts:", signal_counts)
    print("Fechas con FULL:", len(entries_by_date))

    valid_pairs = [
        (tr, cap) for tr in TRADE_RISKS for cap in PORTFOLIO_CAPS
        if cap + 1e-9 >= tr and cap <= MAX_POSITIONS * tr + 1e-9
    ]

    rows, details = [], {}
    for idx, (trade_risk, portfolio_cap) in enumerate(valid_pairs, 1):
        print(f"[{idx:03d}/{len(valid_pairs)}] risk={trade_risk:.2f}% cap={portfolio_cap:.2f}%")
        summary, trades, eq = simulate_configuration(
            entries_by_date, price_maps, all_dates, trade_risk, portfolio_cap
        )
        rows.append(summary)
        key = f"r{trade_risk:.2f}_p{portfolio_cap:.2f}"
        details[key] = {"summary": summary, "trades": trades, "equity_curve": eq.to_dict("records")}

    result_df = pd.DataFrame(rows).sort_values(
        ["calmar", "cagr_pct", "max_drawdown_pct"], ascending=[False, False, False]
    ).reset_index(drop=True)
    frontier = pareto_front(result_df)

    baseline = result_df[
        np.isclose(result_df["trade_risk_pct"], 1.5) &
        np.isclose(result_df["portfolio_risk_cap_pct"], 4.5)
    ]
    baseline_record = baseline.iloc[0].to_dict() if len(baseline) else None

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result_df.to_csv(out_dir / "risk_matrix_results.csv", index=False)
    frontier.to_csv(out_dir / "risk_matrix_pareto.csv", index=False)

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "strategy_version": STRATEGY_VERSION,
        "model_version": MODEL_VERSION,
        "historical_proxy_notes": [
            "Entrada histórica en siguiente Open; producción entra en primera vela 5m posterior al análisis.",
            "fund_score actual aplicado retroactivamente: proxy con sesgo conocido.",
            "OHLC diario usa resolución conservadora SL/BE primero.",
            "Sizing sin apalancamiento: se rechaza si no hay cash para el tamaño completo.",
            "TP1 libera riesgo planificado al mover el remanente a break-even.",
        ],
        "frozen_strategy": {
            "dd60_max_pct": -12.0, "rsi_max": 40.0, "macd_improving": True,
            "volume_decreasing": True, "fund_score_min": 6.5,
            "spy20_max_pct": 1.0, "abnormal20_max_pct": -10.0,
            "stop_pct": -5.0, "tp1_atr_mult": 1.0, "tp2_atr_mult": 1.5,
            "partial_exit": "50/50", "after_tp1_stop": "BREAK_EVEN",
            "time_stop_sessions": 7, "gap_rule": "EXIT_AT_OPEN_IF_OPEN_BELOW_STOP",
            "intraday_conflict": "SL_FIRST", "max_positions": MAX_POSITIONS,
        },
        "matrix": {
            "trade_risks_pct": TRADE_RISKS, "portfolio_caps_pct": PORTFOLIO_CAPS,
            "configurations_tested": int(len(result_df)),
        },
        "signal_counts": signal_counts,
        "date_range": {
            "start": all_dates[0] if all_dates else None,
            "end": all_dates[-1] if all_dates else None,
            "years_requested": args.years, "tickers_with_prices": len(prices),
        },
        "baseline_current_1_5x3": baseline_record,
        "best_by_calmar": result_df.iloc[0].to_dict() if len(result_df) else None,
        "best_by_cagr": result_df.sort_values("cagr_pct", ascending=False).iloc[0].to_dict() if len(result_df) else None,
        "lowest_mdd": result_df.assign(abs_mdd=result_df["max_drawdown_pct"].abs()).sort_values("abs_mdd").drop(columns="abs_mdd").iloc[0].to_dict() if len(result_df) else None,
        "pareto_front": frontier.to_dict("records"),
        "results": result_df.to_dict("records"),
    }
    with open(out_dir / "risk_matrix_summary.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)

    # Detalle completo solo del baseline y del mejor Calmar para no inflar el repo.
    keep_keys = set()
    if baseline_record:
        keep_keys.add(f"r{float(baseline_record['trade_risk_pct']):.2f}_p{float(baseline_record['portfolio_risk_cap_pct']):.2f}")
    if len(result_df):
        best = result_df.iloc[0]
        keep_keys.add(f"r{float(best['trade_risk_pct']):.2f}_p{float(best['portfolio_risk_cap_pct']):.2f}")
    with open(out_dir / "risk_matrix_details.json", "w", encoding="utf-8") as fh:
        json.dump({k: details[k] for k in keep_keys if k in details}, fh, ensure_ascii=False, indent=2, default=str)

    cols = [
        "trade_risk_pct", "portfolio_risk_cap_pct", "cagr_pct",
        "max_drawdown_pct", "calmar", "sharpe", "trades",
        "rejected_risk_cap", "rejected_cash",
    ]
    print("\nTOP 10 CALMAR")
    print(result_df[cols].head(10).to_string(index=False))
    print("\nBASELINE 1.5% / 4.5%")
    print(baseline[cols].to_string(index=False) if len(baseline) else "No disponible")
    print("\nGuardado en", out_dir)


if __name__ == "__main__":
    main()
