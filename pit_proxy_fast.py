"""Fast state cache for the annual yFinance PIT proxy.

Annual statement inputs only change when a fiscal-period column becomes
eligible after the conservative publication lag. Cache those states once per
ticker; derive daily PE/PB from historical prices at scoring time.
"""
from __future__ import annotations

from bisect import bisect_right
import numpy as np
import pandas as pd

import yfinance_pit_proxy as pit


def _change_dates(statements: dict, start: str, end: str, lag_days: int = 90) -> list[str]:
    dates = {start}
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    for df in (statements.get("inc"), statements.get("bal"), statements.get("cf")):
        if df is None or df.empty:
            continue
        for c in df.columns:
            try:
                eff = pd.Timestamp(c).tz_localize(None) + pd.Timedelta(days=lag_days)
            except Exception:
                continue
            if lo < eff <= hi:
                dates.add(eff.date().isoformat())
    return sorted(dates)


def statement_state(statements: dict, asof: str) -> dict:
    inc, bal, cf = statements.get("inc"), statements.get("bal"), statements.get("cf")
    revenue, revenue_prev, income_period = pit._latest_two(inc, ["Total Revenue", "Operating Revenue", "Revenue"], asof)
    ni, ni_prev, _ = pit._latest_two(inc, ["Net Income", "Net Income Common Stockholders", "Net Income Including Noncontrolling Interests"], asof)
    eps, eps_prev, _ = pit._latest_two(inc, ["Diluted EPS", "Basic EPS"], asof)
    equity, balance_period = pit._latest(bal, ["Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity"], asof)
    ca, _ = pit._latest(bal, ["Current Assets", "Total Current Assets"], asof)
    cl, _ = pit._latest(bal, ["Current Liabilities", "Total Current Liabilities"], asof)
    debt, _ = pit._latest(bal, ["Total Debt", "Total Debt And Capital Lease Obligation", "Long Term Debt And Capital Lease Obligation"], asof)
    shares, _ = pit._latest(bal, ["Ordinary Shares Number", "Share Issued", "Common Stock Shares Outstanding"], asof)
    fcf, cf_period = pit._latest(cf, ["Free Cash Flow"], asof)
    if pd.isna(fcf):
        ocf, cf_period = pit._latest(cf, ["Operating Cash Flow", "Total Cash From Operating Activities"], asof)
        capex, _ = pit._latest(cf, ["Capital Expenditure", "Capital Expenditures"], asof)
        if pd.notna(ocf) and pd.notna(capex):
            fcf = float(ocf + capex if capex < 0 else ocf - capex)

    revenue_growth = ((revenue-revenue_prev)/abs(revenue_prev)) if pd.notna(revenue) and pd.notna(revenue_prev) and revenue_prev != 0 else np.nan
    eps_growth = ((eps-eps_prev)/abs(eps_prev)) if pd.notna(eps) and pd.notna(eps_prev) and eps_prev != 0 else np.nan
    if pd.isna(eps_growth) and pd.notna(ni) and pd.notna(ni_prev) and ni_prev != 0:
        eps_growth = (ni-ni_prev)/abs(ni_prev)
    return {
        "income_period_used": income_period.date().isoformat() if income_period is not None else None,
        "balance_period_used": balance_period.date().isoformat() if balance_period is not None else None,
        "cashflow_period_used": cf_period.date().isoformat() if cf_period is not None else None,
        "revenue_growth": revenue_growth,
        "eps_growth": eps_growth,
        "roe": pit._safe_div(ni, equity),
        "debt_equity": pit._safe_div(debt, equity, 100.0),
        "current_ratio": pit._safe_div(ca, cl),
        "fcf_ni_ratio": pit._safe_div(fcf, ni),
        "eps": eps,
        "equity": equity,
        "shares": shares,
        "fcf": fcf,
    }


def build_state_cache(statement_cache: dict, start: str, end: str, lag_days: int = 90):
    cache = {}
    for ticker, statements in statement_cache.items():
        eff_dates = _change_dates(statements, start, end, lag_days)
        states = [(d, statement_state(statements, d)) for d in eff_dates]
        cache[ticker] = {"dates": [x[0] for x in states], "states": [x[1] for x in states]}
    return cache


def state_asof(state_cache: dict, ticker: str, asof: str) -> dict | None:
    item = state_cache.get(ticker)
    if not item or not item["dates"]:
        return None
    i = bisect_right(item["dates"], asof) - 1
    return item["states"][i] if i >= 0 else None


def row_asof(state_cache: dict, ticker: str, sector: str, asof: str, price: float) -> dict:
    st = state_asof(state_cache, ticker, asof)
    if not st:
        return {"ticker": ticker, "sector": sector, "metric_coverage": 0}
    eps, equity, shares = st.get("eps"), st.get("equity"), st.get("shares")
    market_cap = price * shares if pd.notna(price) and pd.notna(shares) else np.nan
    pe = pit._safe_div(price, eps)
    # Match production/yFinance behavior more closely: loss-making firms normally
    # have no trailing PE rather than a negative valuation multiple.
    if pd.notna(eps) and eps <= 0:
        pe = np.nan
    pb = pit._safe_div(market_cap, equity)
    vals = [st.get("revenue_growth"), st.get("eps_growth"), st.get("roe"), st.get("debt_equity"),
            st.get("current_ratio"), st.get("fcf_ni_ratio"), pe, pb]
    return {
        "ticker": ticker,
        "sector": sector,
        "asof_date": asof,
        "metric_coverage": sum(pd.notna(v) for v in vals),
        "revenue_growth": st.get("revenue_growth"),
        "eps_growth": st.get("eps_growth"),
        "roe": st.get("roe"),
        "debt_equity": st.get("debt_equity"),
        "current_ratio": st.get("current_ratio"),
        "fcf_ni_ratio": st.get("fcf_ni_ratio"),
        "pe": pe,
        "pb": pb,
        "fcf": st.get("fcf"),
        "market_cap": market_cap,
    }
