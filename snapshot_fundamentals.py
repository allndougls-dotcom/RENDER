"""Create a compact point-in-time snapshot from the latest STOCK-RADAR export.

The production export is replaced every run. This script preserves only the
fundamental inputs needed to reconstruct SIDI's score later, keeping repository
growth modest while making future backtests truly point-in-time.
"""
from __future__ import annotations

import re
from pathlib import Path
import pandas as pd

MASTER = Path("data/master")
HISTORY = Path("data/history/fundamentals")

KEEP = [
    "ticker", "company", "sector", "industry",
    "revenue_growth", "eps_growth", "roe", "debt_equity", "current_ratio",
    "fcf_ni_ratio", "pe", "pb", "fcf", "market_cap",
    "fund_score", "fund_growth", "fund_solidity", "fund_valuation",
    "warnings", "warning_count",
]


def main() -> None:
    exports = sorted(MASTER.glob("sp500_full_export_*.csv"))
    if not exports:
        raise SystemExit("No se encontró data/master/sp500_full_export_*.csv")

    src = exports[-1]
    m = re.search(r"(\d{8})", src.stem)
    if not m:
        raise SystemExit(f"No se pudo extraer YYYYMMDD de {src.name}")
    stamp = m.group(1)

    df = pd.read_csv(src)
    cols = [c for c in KEEP if c in df.columns]
    if "ticker" not in cols:
        raise SystemExit("El export no contiene columna ticker")

    out = df[cols].copy()
    out.insert(0, "snapshot_date", pd.to_datetime(stamp, format="%Y%m%d").date().isoformat())
    out = out.sort_values("ticker").reset_index(drop=True)

    HISTORY.mkdir(parents=True, exist_ok=True)
    dest = HISTORY / f"fundamentals_{stamp}.csv"
    out.to_csv(dest, index=False)
    print(f"Snapshot PIT guardado: {dest} ({len(out)} tickers, {len(out.columns)} columnas)")


if __name__ == "__main__":
    main()
