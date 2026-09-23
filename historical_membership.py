"""Partial point-in-time S&P 500 membership correction.

The current master export contains `date_added`. This lets historical backtests
exclude a CURRENT constituent before it actually joined the S&P 500.

Important: this is a one-sided survivorship correction. It does NOT restore
companies that were members historically but were removed before the current
universe snapshot. Therefore it reduces, but does not eliminate, survivorship
bias.
"""
from __future__ import annotations

from pathlib import Path
import pandas as pd


def load_date_added() -> dict[str, str | None]:
    files = sorted(Path("data/master").glob("sp500_full_export_*.csv"))
    if not files:
        return {}
    df = pd.read_csv(files[-1], usecols=lambda c: c in {"ticker", "date_added"})
    out = {}
    for row in df.itertuples(index=False):
        ticker = str(row.ticker).upper()
        raw = getattr(row, "date_added", None)
        if raw is None or pd.isna(raw) or not str(raw).strip():
            out[ticker] = None
            continue
        try:
            out[ticker] = pd.Timestamp(raw).date().isoformat()
        except Exception:
            out[ticker] = None
    return out


def filter_signal_map(signal_map: dict, date_added: dict[str, str | None]):
    """Keep signals only on/after the constituent's known S&P entry date."""
    out = {}
    kept = removed = unknown = 0
    for ticker, sigs in signal_map.items():
        added = date_added.get(ticker)
        if added is None:
            unknown += len(sigs)
        for date, sig in sigs.items():
            if added is not None and date < added:
                removed += 1
                continue
            out.setdefault(ticker, {})[date] = sig
            kept += 1
    return out, {
        "kept": kept,
        "removed_pre_membership": removed,
        "unknown_date_signals_kept": unknown,
    }
