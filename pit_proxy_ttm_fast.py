"""Fast state cache for the quarterly/TTM PIT proxy.

Quarterly/annual accounting inputs change only when a new statement period
becomes eligible after the conservative publication lag. Cache those states
once per ticker; derive daily PE/PB from historical price at scoring time.
"""
from __future__ import annotations

from bisect import bisect_right
import numpy as np
import pandas as pd

import pit_proxy_ttm as ttm
import yfinance_pit_proxy as annual


def _change_dates(statements: dict, start: str, end: str) -> list[str]:
    dates = {start}
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    specs = [
        ("qinc", ttm.QUARTER_LAG_DAYS),
        ("qbal", ttm.QUARTER_LAG_DAYS),
        ("qcf", ttm.QUARTER_LAG_DAYS),
        ("inc", ttm.ANNUAL_LAG_DAYS),
        ("bal", ttm.ANNUAL_LAG_DAYS),
        ("cf", ttm.ANNUAL_LAG_DAYS),
    ]
    for key, lag in specs:
        df = statements.get(key)
        if df is None or df.empty:
            continue
        for c in df.columns:
            try:
                eff = pd.Timestamp(c).tz_localize(None) + pd.Timedelta(days=lag)
            except Exception:
                continue
            if lo < eff <= hi:
                dates.add(eff.date().isoformat())
    return sorted(dates)


def build_state_cache(statement_cache: dict, start: str, end: str):
    cache = {}
    for ticker, statements in statement_cache.items():
        eff_dates = _change_dates(statements, start, end)
        states = []
        for d in eff_dates:
            st = ttm.metrics_asof(statements, d, np.nan)
            states.append((d, st))
        cache[ticker] = {
            "dates": [d for d, _ in states],
            "states": [s for _, s in states],
        }
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

    shares = st.get("shares", np.nan)
    equity = st.get("equity", np.nan)
    eps_ttm = st.get("eps_ttm", np.nan)
    market_cap = price * shares if pd.notna(price) and pd.notna(shares) else np.nan
    pe = annual._safe_div(price, eps_ttm)
    if pd.notna(eps_ttm) and eps_ttm <= 0:
        pe = np.nan
    pb = annual._safe_div(market_cap, equity)

    vals = [
        st.get("revenue_growth"), st.get("eps_growth"), st.get("roe"),
        st.get("debt_equity"), st.get("current_ratio"), st.get("fcf_ni_ratio"),
        pe, pb,
    ]
    return {
        "ticker": ticker,
        "sector": sector,
        "asof_date": asof,
        "metric_coverage": int(sum(pd.notna(v) for v in vals)),
        "revenue_growth": st.get("revenue_growth"),
        "eps_growth": st.get("eps_growth"),
        "roe": st.get("roe"),
        "debt_equity": st.get("debt_equity"),
        "current_ratio": st.get("current_ratio"),
        "fcf_ni_ratio": st.get("fcf_ni_ratio"),
        "pe": pe,
        "pb": pb,
        "fcf": st.get("fcf", np.nan),
        "market_cap": market_cap,
    }
