"""Live market/sector context required by SIDI_INTRADAY_V2.

This module computes, at the close of signal day T:
- ``spy_return_20d``: compounded SPY return over the latest 20 sessions.
- ``abnormal_return_20d``: stock 20-session return unexplained by SPY and
  the stock's GICS-sector ETF, using betas estimated on the preceding
  120 aligned sessions.

The abnormal-return definition mirrors the frozen backtest:
    r_stock = alpha + beta_m * r_SPY + beta_s * r_sector
Betas/alpha are estimated on sessions [-140:-20]. The recent 20-session
abnormal return is realised minus model-implied return over [-20:].

If data are incomplete, ``sidi_context_ready`` is False and the ticker cannot
become a FULL SIDI_INTRADAY_V2 setup.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

STRATEGY_VERSION = "SIDI_INTRADAY_V2"
TRAIN_SESSIONS = 120
RECENT_SESSIONS = 20
TOTAL_SESSIONS = TRAIN_SESSIONS + RECENT_SESSIONS

SECTOR_ETF_MAP = {
    "Information Technology": "XLK",
    "Technology": "XLK",
    "Financials": "XLF",
    "Financial Services": "XLF",
    "Health Care": "XLV",
    "Healthcare": "XLV",
    "Energy": "XLE",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}


def _close_series(df: pd.DataFrame) -> pd.Series:
    if df is None or len(df) == 0 or "Close" not in df.columns:
        return pd.Series(dtype=float)
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = pd.to_numeric(close, errors="coerce")
    if isinstance(df.index, pd.DatetimeIndex):
        idx = pd.to_datetime(df.index, errors="coerce")
    elif "Date" in df.columns:
        idx = pd.to_datetime(df["Date"], errors="coerce")
    else:
        idx = pd.to_datetime(df.index, errors="coerce")
    out = pd.Series(close.to_numpy(dtype=float), index=idx, dtype=float)
    out = out[~out.index.isna()].dropna()
    out.index = out.index.tz_localize(None).normalize()
    return out[~out.index.duplicated(keep="last")].sort_index()


def _download_benchmarks() -> dict[str, pd.Series]:
    tickers = ["SPY"] + sorted(set(SECTOR_ETF_MAP.values()))
    end = (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=430)).strftime("%Y-%m-%d")
    out: dict[str, pd.Series] = {}
    try:
        raw = yf.download(
            tickers, start=start, end=end, auto_adjust=True,
            progress=False, group_by="ticker", threads=True,
        )
        for ticker in tickers:
            try:
                df_t = raw[ticker].copy() if isinstance(raw.columns, pd.MultiIndex) else raw.copy()
                s = _close_series(df_t)
                if len(s) >= TOTAL_SESSIONS + 1:
                    out[ticker] = s
            except Exception:
                continue
    except Exception as exc:
        print(f"  ⚠ SIDI context: no se pudieron descargar benchmarks ({exc})")
    return out


def _spy20(spy_close: pd.Series) -> float:
    if len(spy_close) < RECENT_SESSIONS + 1:
        return np.nan
    recent = spy_close.iloc[-(RECENT_SESSIONS + 1):]
    first, last = float(recent.iloc[0]), float(recent.iloc[-1])
    if first <= 0:
        return np.nan
    return (last / first - 1.0) * 100.0


def _abnormal20(stock_close: pd.Series, spy_close: pd.Series,
                sector_close: pd.Series) -> tuple[float, float, float, int]:
    stock_r = stock_close.pct_change().rename("stock")
    spy_r = spy_close.pct_change().rename("spy")
    sector_r = sector_close.pct_change().rename("sector")
    aligned = pd.concat([stock_r, spy_r, sector_r], axis=1, join="inner").dropna()
    if len(aligned) < TOTAL_SESSIONS:
        return np.nan, np.nan, np.nan, int(len(aligned))
    sample = aligned.tail(TOTAL_SESSIONS)
    train = sample.iloc[:TRAIN_SESSIONS]
    recent = sample.iloc[TRAIN_SESSIONS:]
    if len(train) != TRAIN_SESSIONS or len(recent) != RECENT_SESSIONS:
        return np.nan, np.nan, np.nan, int(len(aligned))
    x = np.column_stack([
        np.ones(len(train), dtype=float),
        train["spy"].to_numpy(dtype=float),
        train["sector"].to_numpy(dtype=float),
    ])
    y = train["stock"].to_numpy(dtype=float)
    try:
        alpha, beta_spy, beta_sector = np.linalg.lstsq(x, y, rcond=None)[0]
    except Exception:
        return np.nan, np.nan, np.nan, int(len(aligned))
    actual = float(recent["stock"].sum())
    expected = float(
        alpha * len(recent)
        + beta_spy * recent["spy"].sum()
        + beta_sector * recent["sector"].sum()
    )
    abnormal = (actual - expected) * 100.0
    return abnormal, float(beta_spy), float(beta_sector), int(len(aligned))


def calcular_contexto_sidi(all_prices: dict, sp500: pd.DataFrame) -> pd.DataFrame:
    print("  ⏳ SIDI_INTRADAY_V2 · calculando SPY20 + Abnormal20...")
    benchmarks = _download_benchmarks()
    spy_close = benchmarks.get("SPY", pd.Series(dtype=float))
    spy20 = _spy20(spy_close)
    sector_by_ticker = {}
    if sp500 is not None and len(sp500) and {"ticker", "sector"}.issubset(sp500.columns):
        sector_by_ticker = dict(zip(sp500["ticker"].astype(str), sp500["sector"].astype(str)))
    rows = []
    ready_count = 0
    for ticker, df in all_prices.items():
        sector = sector_by_ticker.get(str(ticker), "")
        sector_etf = SECTOR_ETF_MAP.get(sector)
        sector_close = benchmarks.get(sector_etf, pd.Series(dtype=float)) if sector_etf else pd.Series(dtype=float)
        stock_close = _close_series(df)
        abnormal, beta_spy, beta_sector, aligned_obs = _abnormal20(
            stock_close, spy_close, sector_close
        ) if (len(spy_close) and len(sector_close)) else (np.nan, np.nan, np.nan, 0)
        ready = bool(np.isfinite(spy20) and np.isfinite(abnormal))
        ready_count += int(ready)
        rows.append({
            "ticker": str(ticker),
            "sidi_strategy_version": STRATEGY_VERSION,
            "spy_return_20d": round(float(spy20), 4) if np.isfinite(spy20) else np.nan,
            "abnormal_return_20d": round(float(abnormal), 4) if np.isfinite(abnormal) else np.nan,
            "abnormal_beta_spy": round(float(beta_spy), 4) if np.isfinite(beta_spy) else np.nan,
            "abnormal_beta_sector": round(float(beta_sector), 4) if np.isfinite(beta_sector) else np.nan,
            "abnormal_aligned_obs": int(aligned_obs),
            "abnormal_sector_etf": sector_etf or "",
            "sidi_context_ready": ready,
        })
    print(
        f"  ✅ SIDI context: {ready_count}/{len(rows)} tickers listos"
        + (f" · SPY20 {spy20:+.2f}%" if np.isfinite(spy20) else " · SPY20 N/D")
    )
    return pd.DataFrame(rows)
