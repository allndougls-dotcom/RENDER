"""Fixed runner for quarterly/TTM calibration.

Archived compact snapshots do not store raw price. For calibration only, derive
the contemporaneous price from archived market_cap divided by the reconstructed
latest shares outstanding. This keeps valuation on the archived date without
requiring another price feed and leaves the historical backtest methodology
unchanged.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import calibrate_pit_proxy_ttm as base
import pit_proxy_ttm as ttm
from modules.ingesta.scoring import _sector_medians, _fund_score


def fixed_proxy_for_snapshot(real: pd.DataFrame, asof: str, tickers: list[str], sectors: dict,
                             cache: dict, added: dict[str, str | None]) -> pd.DataFrame:
    mcaps = dict(zip(real.ticker.astype(str), pd.to_numeric(real["market_cap"], errors="coerce")))
    active = [t for t in tickers if (added.get(t) is None or asof >= added[t]) and t in cache]
    rows = []
    for ticker in active:
        # First pass obtains eligible shares; valuation fields are intentionally NaN.
        pre = ttm.metrics_asof(cache[ticker], asof, np.nan)
        shares = pre.get("shares", np.nan)
        mcap = mcaps.get(ticker, np.nan)
        price = float(mcap / shares) if pd.notna(mcap) and pd.notna(shares) and shares != 0 else np.nan
        m = ttm.metrics_asof(cache[ticker], asof, price)
        m.update({"ticker": ticker, "sector": sectors.get(ticker, "Unknown"), "asof_date": asof})
        rows.append(m)
    df = pd.DataFrame(rows)
    sm = _sector_medians(df)
    scores = df.apply(lambda r: _fund_score(r, sm), axis=1)
    return pd.concat([df, scores], axis=1)


base.proxy_for_snapshot = fixed_proxy_for_snapshot

if __name__ == "__main__":
    base.main()
