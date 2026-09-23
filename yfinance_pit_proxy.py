"""Conservative yFinance point-in-time FUNDAMENTAL PROXY for SIDI.

Why this exists
---------------
GitHub-hosted runners receive HTTP 403 from SEC companyfacts. This module
provides a reproducible fallback that is materially stricter than applying
TODAY'S fund_score to the past.

Method
------
* Uses historical annual statement columns exposed by yFinance.
* A fiscal period is eligible only after a conservative 90-day publication lag:
      fiscal_period_end + 90 days <= asof_date
* Growth uses the two latest eligible annual periods.
* Balance/cash-flow values use only eligible fiscal periods.
* Valuation uses historical price on/before as-of date.
* Then the existing SIDI sector-normalised scoring formula can be applied.

Limitations
-----------
This is NOT true filing-date point-in-time data. yFinance may expose restated
historical values and the current constituent universe still creates
survivorship bias. Results must therefore be labelled PIT_PROXY, not PIT_TRUE.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import yfinance as yf

PUBLICATION_LAG_DAYS = 90


def _norm_label(x) -> str:
    return " ".join(str(x).lower().replace("_", " ").replace("-", " ").split())


def _find_row(df: pd.DataFrame, candidates: Iterable[str]) -> pd.Series | None:
    if df is None or df.empty:
        return None
    idx = {_norm_label(i): i for i in df.index}
    # Exact normalized match first.
    for cand in candidates:
        key = _norm_label(cand)
        if key in idx:
            return df.loc[idx[key]]
    # Then conservative contains-match for vendor label drift.
    for cand in candidates:
        key = _norm_label(cand)
        for norm, original in idx.items():
            if key in norm or norm in key:
                return df.loc[original]
    return None


def _eligible_columns(df: pd.DataFrame, asof: str, lag_days: int = PUBLICATION_LAG_DAYS) -> list[pd.Timestamp]:
    if df is None or df.empty:
        return []
    cutoff = pd.Timestamp(asof)
    cols = []
    for c in df.columns:
        try:
            d = pd.Timestamp(c).tz_localize(None)
        except Exception:
            continue
        if d + pd.Timedelta(days=lag_days) <= cutoff:
            cols.append(d)
    return sorted(cols, reverse=True)


def _value(row: pd.Series | None, col: pd.Timestamp) -> float:
    if row is None:
        return np.nan
    # yFinance columns can be Timestamp-like but not object-identical.
    for c in row.index:
        try:
            if pd.Timestamp(c).tz_localize(None) == col:
                v = row[c]
                return float(v) if pd.notna(v) else np.nan
        except Exception:
            continue
    return np.nan


def _latest(df: pd.DataFrame, candidates: list[str], asof: str) -> tuple[float, pd.Timestamp | None]:
    cols = _eligible_columns(df, asof)
    row = _find_row(df, candidates)
    for col in cols:
        v = _value(row, col)
        if pd.notna(v):
            return v, col
    return np.nan, None


def _latest_two(df: pd.DataFrame, candidates: list[str], asof: str) -> tuple[float, float, pd.Timestamp | None]:
    cols = _eligible_columns(df, asof)
    row = _find_row(df, candidates)
    vals = []
    for col in cols:
        v = _value(row, col)
        if pd.notna(v):
            vals.append((v, col))
        if len(vals) >= 2:
            break
    if not vals:
        return np.nan, np.nan, None
    latest, period = vals[0]
    prior = vals[1][0] if len(vals) > 1 else np.nan
    return latest, prior, period


def _safe_div(a, b, scale=1.0):
    if pd.isna(a) or pd.isna(b) or b == 0:
        return np.nan
    return float(a / b * scale)


def _historical_close(ticker: str, asof: str) -> float:
    d = pd.Timestamp(asof)
    try:
        hist = yf.download(
            ticker.replace("-", "."),
            start=(d - pd.Timedelta(days=12)).date().isoformat(),
            end=(d + pd.Timedelta(days=1)).date().isoformat(),
            progress=False,
            auto_adjust=True,
            threads=False,
        )
        if hist.empty:
            return np.nan
        close = hist["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()
        return float(close.iloc[-1]) if len(close) else np.nan
    except Exception:
        return np.nan


def reconstruct_one(ticker: str, asof: str, sector: str | None = None, lag_days: int = PUBLICATION_LAG_DAYS) -> dict:
    global PUBLICATION_LAG_DAYS
    old_lag = PUBLICATION_LAG_DAYS
    PUBLICATION_LAG_DAYS = lag_days
    try:
        t = yf.Ticker(ticker.replace("-", "."))
        inc = t.income_stmt
        bal = t.balance_sheet
        cf = t.cashflow

        revenue, revenue_prev, income_period = _latest_two(
            inc, ["Total Revenue", "Operating Revenue", "Revenue"], asof
        )
        ni, ni_prev, _ = _latest_two(
            inc, ["Net Income", "Net Income Common Stockholders", "Net Income Including Noncontrolling Interests"], asof
        )
        eps, eps_prev, _ = _latest_two(
            inc, ["Diluted EPS", "Basic EPS"], asof
        )

        equity, balance_period = _latest(
            bal, ["Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity"], asof
        )
        current_assets, _ = _latest(bal, ["Current Assets", "Total Current Assets"], asof)
        current_liab, _ = _latest(bal, ["Current Liabilities", "Total Current Liabilities"], asof)
        total_debt, _ = _latest(
            bal, ["Total Debt", "Total Debt And Capital Lease Obligation", "Long Term Debt And Capital Lease Obligation"], asof
        )
        shares, _ = _latest(
            bal, ["Ordinary Shares Number", "Share Issued", "Common Stock Shares Outstanding"], asof
        )

        fcf, cf_period = _latest(cf, ["Free Cash Flow"], asof)
        if pd.isna(fcf):
            ocf, cf_period = _latest(cf, ["Operating Cash Flow", "Total Cash From Operating Activities"], asof)
            capex, _ = _latest(cf, ["Capital Expenditure", "Capital Expenditures"], asof)
            if pd.notna(ocf) and pd.notna(capex):
                # yFinance commonly reports capex as a negative cash outflow.
                fcf = float(ocf + capex if capex < 0 else ocf - capex)

        price = _historical_close(ticker, asof)
        market_cap = price * shares if pd.notna(price) and pd.notna(shares) else np.nan

        revenue_growth = (
            (revenue - revenue_prev) / abs(revenue_prev)
            if pd.notna(revenue) and pd.notna(revenue_prev) and revenue_prev != 0 else np.nan
        )
        eps_growth = (
            (eps - eps_prev) / abs(eps_prev)
            if pd.notna(eps) and pd.notna(eps_prev) and eps_prev != 0 else np.nan
        )
        # EPS is occasionally absent; NI growth is a labelled fallback, not hidden.
        eps_growth_source = "eps"
        if pd.isna(eps_growth) and pd.notna(ni) and pd.notna(ni_prev) and ni_prev != 0:
            eps_growth = (ni - ni_prev) / abs(ni_prev)
            eps_growth_source = "net_income_proxy"

        roe = _safe_div(ni, equity)
        debt_equity = _safe_div(total_debt, equity, 100.0)
        current_ratio = _safe_div(current_assets, current_liab)
        fcf_ni_ratio = _safe_div(fcf, ni)
        pe = _safe_div(price, eps)
        pb = _safe_div(market_cap, equity)

        metrics = [revenue_growth, eps_growth, roe, debt_equity, current_ratio, fcf_ni_ratio, pe, pb]
        coverage = sum(pd.notna(v) for v in metrics)

        return {
            "asof_date": asof,
            "ticker": ticker,
            "sector": sector,
            "pit_proxy": True,
            "publication_lag_days": lag_days,
            "income_period_used": income_period.date().isoformat() if income_period is not None else None,
            "balance_period_used": balance_period.date().isoformat() if balance_period is not None else None,
            "cashflow_period_used": cf_period.date().isoformat() if cf_period is not None else None,
            "eps_growth_source": eps_growth_source,
            "metric_coverage": coverage,
            "price": price,
            "revenue_growth": revenue_growth,
            "eps_growth": eps_growth,
            "roe": roe,
            "debt_equity": debt_equity,
            "current_ratio": current_ratio,
            "fcf_ni_ratio": fcf_ni_ratio,
            "pe": pe,
            "pb": pb,
            "fcf": fcf,
            "market_cap": market_cap,
            "revenue": revenue,
            "net_income": ni,
            "eps": eps,
            "equity": equity,
            "shares": shares,
        }
    finally:
        PUBLICATION_LAG_DAYS = old_lag


def load_sector_map() -> dict[str, str]:
    files = sorted(Path("data/master").glob("sp500_full_export_*.csv"))
    if not files:
        return {}
    df = pd.read_csv(files[-1], usecols=lambda c: c in {"ticker", "sector"})
    return {str(r.ticker).upper().replace(".", "-"): str(r.sector) for r in df.itertuples()}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--asof", required=True)
    p.add_argument("--tickers", default="AAPL,MSFT,NVDA,JPM,XOM,JNJ,PG,CAT")
    p.add_argument("--lag", type=int, default=90)
    p.add_argument("--out", default="yf_pit_proxy.csv")
    args = p.parse_args()

    pd.Timestamp(args.asof)
    sectors = load_sector_map()
    tickers = [x.strip().upper().replace(".", "-") for x in args.tickers.split(",") if x.strip()]
    rows = []
    for ticker in tickers:
        try:
            row = reconstruct_one(ticker, args.asof, sectors.get(ticker), args.lag)
            rows.append(row)
            print(
                f"{ticker:<5} coverage={row['metric_coverage']}/8 "
                f"period={row['income_period_used']} price={row['price']}"
            )
        except Exception as exc:
            print(f"ERR {ticker}: {type(exc).__name__}: {exc}")
        time.sleep(0.10)

    df = pd.DataFrame(rows)
    df.to_csv(args.out, index=False)
    print(f"Saved {args.out}: {len(df)} rows; mean coverage={df['metric_coverage'].mean() if len(df) else 0:.2f}/8")


if __name__ == "__main__":
    main()
