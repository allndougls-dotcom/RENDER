"""Quarterly/TTM point-in-time proxy for SIDI's yFinance fundamental inputs.

The production pipeline uses ``yf.Ticker(...).info`` fields such as
``revenueGrowth``, ``earningsGrowth``, ``returnOnEquity`` and
``freeCashflow``. Those are mostly recent-quarter / trailing-twelve-month
metrics, so reconstructing them from two annual statements is not faithful.

This module approximates those fields using quarterly statements:
- revenue_growth: latest eligible quarter vs same quarter one year earlier;
- eps_growth: latest eligible diluted EPS vs same quarter one year earlier;
- ROE: TTM net income / latest equity;
- debt/equity and current ratio: latest eligible balance sheet;
- FCF/NI: TTM free cash flow / TTM net income;
- PE: historical price / TTM diluted EPS;
- PB: historical market cap / latest equity.

A quarter becomes usable only after QUARTER_LAG_DAYS. This is still a
PIT_PROXY, not PIT_TRUE, because yFinance can expose restated historical
statements and does not supply the original filing timestamp here.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import yfinance_pit_proxy as annual

QUARTER_LAG_DAYS = 45
ANNUAL_LAG_DAYS = 90


def _cols(df: pd.DataFrame, asof: str, lag_days: int) -> list[pd.Timestamp]:
    if df is None or df.empty:
        return []
    cutoff = pd.Timestamp(asof)
    out = []
    for c in df.columns:
        try:
            d = pd.Timestamp(c).tz_localize(None)
        except Exception:
            continue
        if d + pd.Timedelta(days=lag_days) <= cutoff:
            out.append(d)
    return sorted(set(out), reverse=True)


def _row(df: pd.DataFrame, candidates: list[str]):
    return annual._find_row(df, candidates)


def _v(row, col):
    return annual._value(row, col) if col is not None else np.nan


def _latest(df: pd.DataFrame, candidates: list[str], asof: str, lag_days: int):
    row = _row(df, candidates)
    for c in _cols(df, asof, lag_days):
        v = _v(row, c)
        if pd.notna(v):
            return float(v), c
    return np.nan, None


def _latest_n(df: pd.DataFrame, candidates: list[str], asof: str, lag_days: int, n: int = 4):
    row = _row(df, candidates)
    vals = []
    for c in _cols(df, asof, lag_days):
        v = _v(row, c)
        if pd.notna(v):
            vals.append((float(v), c))
        if len(vals) >= n:
            break
    return vals


def _same_quarter_year_ago(df: pd.DataFrame, candidates: list[str], latest_col: pd.Timestamp,
                           asof: str, lag_days: int):
    if latest_col is None:
        return np.nan, None
    row = _row(df, candidates)
    candidates_cols = _cols(df, asof, lag_days)
    target = latest_col - pd.DateOffset(years=1)
    viable = []
    for c in candidates_cols:
        if c >= latest_col:
            continue
        # Fiscal calendars can shift by a few days/weeks; accept closest period
        # around one year ago, but never an adjacent quarter.
        delta = abs((c - target).days)
        if delta <= 55:
            viable.append((delta, c))
    if not viable:
        return np.nan, None
    viable.sort(key=lambda x: x[0])
    c = viable[0][1]
    return _v(row, c), c


def _growth_latest_q_yoy(df: pd.DataFrame, candidates: list[str], asof: str, lag_days: int):
    latest, latest_col = _latest(df, candidates, asof, lag_days)
    prior, prior_col = _same_quarter_year_ago(df, candidates, latest_col, asof, lag_days)
    if pd.isna(latest) or pd.isna(prior) or prior == 0:
        return np.nan, latest_col, prior_col
    return float((latest - prior) / abs(prior)), latest_col, prior_col


def _ttm_sum(df: pd.DataFrame, candidates: list[str], asof: str, lag_days: int):
    vals = _latest_n(df, candidates, asof, lag_days, 4)
    if len(vals) < 4:
        return np.nan, [c for _, c in vals]
    return float(sum(v for v, _ in vals)), [c for _, c in vals]


def _fcf_ttm(qcf: pd.DataFrame, asof: str, lag_days: int):
    fcf, cols = _ttm_sum(qcf, ["Free Cash Flow"], asof, lag_days)
    if pd.notna(fcf):
        return fcf, cols
    ocf, ocf_cols = _ttm_sum(qcf, ["Operating Cash Flow", "Total Cash From Operating Activities"], asof, lag_days)
    capex, capex_cols = _ttm_sum(qcf, ["Capital Expenditure", "Capital Expenditures"], asof, lag_days)
    if pd.notna(ocf) and pd.notna(capex):
        # yFinance typically reports capex as negative cash outflow.
        return float(ocf + capex if capex < 0 else ocf - capex), ocf_cols
    return np.nan, ocf_cols or capex_cols


def metrics_asof(statements: dict, asof: str, price: float,
                 quarter_lag: int = QUARTER_LAG_DAYS,
                 annual_lag: int = ANNUAL_LAG_DAYS) -> dict:
    qinc = statements.get("qinc")
    qbal = statements.get("qbal")
    qcf = statements.get("qcf")
    ainc = statements.get("inc")
    abal = statements.get("bal")
    acf = statements.get("cf")

    revenue_growth, rev_q, rev_prior_q = _growth_latest_q_yoy(
        qinc, ["Total Revenue", "Operating Revenue", "Revenue"], asof, quarter_lag)
    eps_growth, eps_q, eps_prior_q = _growth_latest_q_yoy(
        qinc, ["Diluted EPS", "Basic EPS"], asof, quarter_lag)

    ni_ttm, ni_cols = _ttm_sum(
        qinc, ["Net Income", "Net Income Common Stockholders", "Net Income Including Noncontrolling Interests"],
        asof, quarter_lag)
    eps_ttm, eps_cols = _ttm_sum(qinc, ["Diluted EPS", "Basic EPS"], asof, quarter_lag)
    fcf_ttm, fcf_cols = _fcf_ttm(qcf, asof, quarter_lag)

    equity, bal_q = _latest(qbal, ["Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity"], asof, quarter_lag)
    ca, _ = _latest(qbal, ["Current Assets", "Total Current Assets"], asof, quarter_lag)
    cl, _ = _latest(qbal, ["Current Liabilities", "Total Current Liabilities"], asof, quarter_lag)
    debt, _ = _latest(qbal, ["Total Debt", "Total Debt And Capital Lease Obligation", "Long Term Debt And Capital Lease Obligation"], asof, quarter_lag)
    shares, _ = _latest(qbal, ["Ordinary Shares Number", "Share Issued", "Common Stock Shares Outstanding"], asof, quarter_lag)

    # Conservative annual fallbacks when quarterly history is incomplete.
    if pd.isna(revenue_growth):
        rv, rp, _ = annual._latest_two(ainc, ["Total Revenue", "Operating Revenue", "Revenue"], asof)
        revenue_growth = (rv-rp)/abs(rp) if pd.notna(rv) and pd.notna(rp) and rp != 0 else np.nan
    if pd.isna(eps_growth):
        ev, ep, _ = annual._latest_two(ainc, ["Diluted EPS", "Basic EPS"], asof)
        eps_growth = (ev-ep)/abs(ep) if pd.notna(ev) and pd.notna(ep) and ep != 0 else np.nan
    if pd.isna(ni_ttm):
        ni_ttm, _ = annual._latest(ainc, ["Net Income", "Net Income Common Stockholders", "Net Income Including Noncontrolling Interests"], asof)
    if pd.isna(eps_ttm):
        eps_ttm, _ = annual._latest(ainc, ["Diluted EPS", "Basic EPS"], asof)
    if pd.isna(fcf_ttm):
        fcf_ttm, _ = annual._latest(acf, ["Free Cash Flow"], asof)
    if pd.isna(equity):
        equity, _ = annual._latest(abal, ["Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity"], asof)
    if pd.isna(ca):
        ca, _ = annual._latest(abal, ["Current Assets", "Total Current Assets"], asof)
    if pd.isna(cl):
        cl, _ = annual._latest(abal, ["Current Liabilities", "Total Current Liabilities"], asof)
    if pd.isna(debt):
        debt, _ = annual._latest(abal, ["Total Debt", "Total Debt And Capital Lease Obligation", "Long Term Debt And Capital Lease Obligation"], asof)
    if pd.isna(shares):
        shares, _ = annual._latest(abal, ["Ordinary Shares Number", "Share Issued", "Common Stock Shares Outstanding"], asof)

    market_cap = price * shares if pd.notna(price) and pd.notna(shares) else np.nan
    roe = annual._safe_div(ni_ttm, equity)
    debt_equity = annual._safe_div(debt, equity, 100.0)
    current_ratio = annual._safe_div(ca, cl)
    fcf_ni_ratio = annual._safe_div(fcf_ttm, ni_ttm)
    pe = annual._safe_div(price, eps_ttm)
    if pd.notna(eps_ttm) and eps_ttm <= 0:
        pe = np.nan
    pb = annual._safe_div(market_cap, equity)

    vals = [revenue_growth, eps_growth, roe, debt_equity, current_ratio, fcf_ni_ratio, pe, pb]
    return {
        "revenue_growth": revenue_growth,
        "eps_growth": eps_growth,
        "roe": roe,
        "debt_equity": debt_equity,
        "current_ratio": current_ratio,
        "fcf_ni_ratio": fcf_ni_ratio,
        "pe": pe,
        "pb": pb,
        "fcf": fcf_ttm,
        "market_cap": market_cap,
        "metric_coverage": int(sum(pd.notna(v) for v in vals)),
        "latest_growth_quarter": rev_q.date().isoformat() if rev_q is not None else None,
        "prior_growth_quarter": rev_prior_q.date().isoformat() if rev_prior_q is not None else None,
        "eps_growth_quarter": eps_q.date().isoformat() if eps_q is not None else None,
        "balance_quarter": bal_q.date().isoformat() if bal_q is not None else None,
    }
