"""Backfill compact fundamental snapshots from historical Git commits.

This recovers the daily STOCK-RADAR exports that were deleted from the working
tree but remain addressable in Git history. It never fabricates older data: a
snapshot is written only when that exact dated export existed in a commit.
"""
from __future__ import annotations

import io
import re
import subprocess
from pathlib import Path

import pandas as pd

from snapshot_fundamentals import KEEP

DEST = Path("data/history/fundamentals")
PATTERN = re.compile(r"data/master/sp500_full_export_(\d{8})\.csv$")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL)


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    # Newest first. If there were multiple refreshes on one date, preserve the last one.
    shas = [line.strip() for line in git("log", "--format=%H", "--", "data/master").splitlines() if line.strip()]
    seen_dates: set[str] = set()
    written = 0

    for sha in shas:
        paths = git("ls-tree", "-r", "--name-only", sha, "data/master").splitlines()
        matches = []
        for path in paths:
            m = PATTERN.match(path.strip())
            if m:
                matches.append((m.group(1), path.strip()))
        if not matches:
            continue

        # A commit should contain one current export. Be defensive if it contains more.
        for stamp, path in sorted(matches, reverse=True):
            if stamp in seen_dates:
                continue
            seen_dates.add(stamp)
            try:
                raw = git("show", f"{sha}:{path}")
                df = pd.read_csv(io.StringIO(raw))
            except Exception as exc:
                print(f"WARN {stamp}: no se pudo leer {path} en {sha[:8]}: {exc}")
                continue

            cols = [c for c in KEEP if c in df.columns]
            if "ticker" not in cols:
                print(f"WARN {stamp}: export sin ticker; omitido")
                continue
            out = df[cols].copy()
            out.insert(0, "snapshot_date", pd.to_datetime(stamp, format="%Y%m%d").date().isoformat())
            out = out.sort_values("ticker").reset_index(drop=True)
            dest = DEST / f"fundamentals_{stamp}.csv"
            out.to_csv(dest, index=False)
            written += 1
            print(f"RECOVERED {stamp}: {len(out)} tickers <- {sha[:8]}")

    print(f"Backfill completo: {written} snapshots únicos recuperados; rango={min(seen_dates) if seen_dates else 'N/A'}..{max(seen_dates) if seen_dates else 'N/A'}")


if __name__ == "__main__":
    main()
