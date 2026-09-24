"""Audit the historical S&P 500 universe for SIDI backtesting.

Purpose
-------
Remove the remaining one-sided survivorship bias by identifying constituents
that were actually in the S&P 500 during the frozen OOS period but are no
longer in today's master universe.

This script does NOT alter production and does NOT change any SIDI parameter.
It only builds/audits a historical membership universe and checks whether
historical price data are still retrievable for removed constituents.

Historical membership source is pinned for reproducibility:
  hanshof/sp500_constituents @ a91ef88fad5ace83bed1f3452f451247295bcd18
  sp_500_historical_components.csv
The source repository documents coverage from 1996 to present.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

import pandas as pd

import backtest as bt

SOURCE_COMMIT = "a91ef88fad5ace83bed1f3452f451247295bcd18"
SOURCE_URL = (
    "https://raw.githubusercontent.com/hanshof/sp500_constituents/"
    f"{SOURCE_COMMIT}/sp_500_historical_components.csv"
)
START = "2020-01-01"
END = "2024-08-10"
PRICE_START = "2019-09-01"
PRICE_END = "2024-08-11"  # yfinance end is exclusive


def norm_ticker(x: str) -> str:
    """Normalise historical symbols to Yahoo/SIDI convention."""
    return str(x).strip().upper().replace(".", "-")


def parse_snapshot(raw: str) -> set[str]:
    if raw is None or pd.isna(raw):
        return set()
    return {norm_ticker(x) for x in str(raw).split(",") if str(x).strip()}


def load_history() -> pd.DataFrame:
    print(f"Downloading historical membership source pinned at {SOURCE_COMMIT}...")
    df = pd.read_csv(SOURCE_URL)
    if "date" not in df.columns or "tickers" not in df.columns:
        raise ValueError(f"Unexpected source columns: {list(df.columns)}")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "tickers"]).sort_values("date")
    df = df.drop_duplicates(subset=["date"], keep="last")
    df["date_str"] = df["date"].dt.date.astype(str)
    return df


def period_rows(df: pd.DataFrame) -> pd.DataFrame:
    # Include the last snapshot before START as the opening state when available.
    start_ts = pd.Timestamp(START)
    end_ts = pd.Timestamp(END)
    before = df[df["date"] <= start_ts]
    if before.empty:
        raise ValueError("Historical source has no snapshot at/before START")
    opening_idx = before.index[-1]
    sub = df.loc[opening_idx:]
    sub = sub[sub["date"] <= end_ts].copy()
    sub["members"] = sub["tickers"].map(parse_snapshot)
    return sub


def build_intervals(rows: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    snapshots = list(rows[["date_str", "members"]].itertuples(index=False, name=None))
    if not snapshots:
        return pd.DataFrame(), pd.DataFrame()

    intervals = []
    events = []
    first_date, first_members = snapshots[0]
    active_start = {t: first_date for t in first_members}
    prev_date = first_date
    prev = set(first_members)

    for date, cur in snapshots[1:]:
        cur = set(cur)
        added = sorted(cur - prev)
        removed = sorted(prev - cur)
        for t in added:
            active_start[t] = date
            events.append({"date": date, "ticker": t, "action": "ADD"})
        for t in removed:
            intervals.append({
                "ticker": t,
                "start_date": active_start.pop(t, first_date),
                "end_date": prev_date,
            })
            events.append({"date": date, "ticker": t, "action": "REMOVE"})
        prev = cur
        prev_date = date

    last_date = snapshots[-1][0]
    for t, st in active_start.items():
        intervals.append({"ticker": t, "start_date": st, "end_date": last_date})

    idf = pd.DataFrame(intervals).sort_values(["ticker", "start_date"]).reset_index(drop=True)
    edf = pd.DataFrame(events).sort_values(["date", "ticker", "action"]).reset_index(drop=True)
    return idf, edf


def snapshot_on_or_before(rows: pd.DataFrame, date: str) -> set[str]:
    sub = rows[rows["date"] <= pd.Timestamp(date)]
    if sub.empty:
        return set()
    return set(sub.iloc[-1]["members"])


def main():
    print("=" * 112)
    print("SIDI — FULL HISTORICAL S&P 500 UNIVERSE AUDIT")
    print("=" * 112)
    print(f"Frozen audit period: {START} -> {END}\n")

    hist = load_history()
    rows = period_rows(hist)
    intervals, events = build_intervals(rows)

    current = {norm_ticker(t) for t in bt.load_tickers()}
    union = set().union(*rows["members"].tolist()) if len(rows) else set()
    removed_only = sorted(union - current)
    current_overlap = sorted(union & current)
    current_not_seen = sorted(current - union)

    sizes = rows["members"].map(len)
    print(f"Source rows in/opening period: {len(rows)}")
    print(f"Source first snapshot used: {rows.iloc[0]['date_str']}")
    print(f"Source last snapshot used:  {rows.iloc[-1]['date_str']}")
    print(f"Membership size: min={sizes.min()} median={sizes.median():.0f} max={sizes.max()}")
    print(f"Historical union: {len(union)} unique tickers")
    print(f"Current universe: {len(current)}")
    print(f"Historical tickers still current: {len(current_overlap)}")
    print(f"Historical members no longer current: {len(removed_only)}")
    print(f"Current names not present in 2020..2024-08-10 union: {len(current_not_seen)}")

    print("\nChecking historical price availability for removed constituents...")
    removed_prices = bt.download_prices(removed_only, start_date=PRICE_START, end_date=PRICE_END) if removed_only else {}
    price_ok = {
        t for t, df in removed_prices.items()
        if df is not None and not df.empty and pd.to_datetime(df["Date"]).max() >= pd.Timestamp(START)
    }
    price_missing = sorted(set(removed_only) - price_ok)
    print(f"Removed tickers with usable prices: {len(price_ok)}/{len(removed_only)}")
    if price_missing:
        print(f"Missing/unusable historical prices ({len(price_missing)}): {', '.join(price_missing)}")

    first_last = (
        intervals.groupby("ticker")
        .agg(first_seen=("start_date", "min"), last_seen=("end_date", "max"), membership_spans=("ticker", "size"))
        .reset_index()
    )
    removed_df = first_last[first_last["ticker"].isin(removed_only)].copy()
    removed_df["price_available"] = removed_df["ticker"].isin(price_ok)
    removed_df = removed_df.sort_values(["price_available", "last_seen", "ticker"], ascending=[False, True, True])

    years = []
    for date in ["2020-01-02", "2021-01-04", "2022-01-03", "2023-01-03", "2024-01-02", END]:
        mem = snapshot_on_or_before(rows, date)
        years.append({
            "date": date,
            "members": len(mem),
            "not_current_today": len(mem - current),
            "still_current_today": len(mem & current),
        })
    annual_df = pd.DataFrame(years)

    intervals.to_csv("sidi_sp500_membership_intervals_2020_2024.csv", index=False)
    events.to_csv("sidi_sp500_membership_events_2020_2024.csv", index=False)
    removed_df.to_csv("sidi_sp500_removed_constituents_2020_2024.csv", index=False)
    annual_df.to_csv("sidi_sp500_membership_annual_audit.csv", index=False)

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "period": [START, END],
        "source": {
            "repository": "hanshof/sp500_constituents",
            "commit": SOURCE_COMMIT,
            "file": "sp_500_historical_components.csv",
            "url": SOURCE_URL,
        },
        "source_rows": int(len(rows)),
        "source_first_snapshot": rows.iloc[0]["date_str"],
        "source_last_snapshot": rows.iloc[-1]["date_str"],
        "membership_size_min": int(sizes.min()),
        "membership_size_median": float(sizes.median()),
        "membership_size_max": int(sizes.max()),
        "historical_unique_tickers": len(union),
        "current_universe": len(current),
        "historical_still_current": len(current_overlap),
        "historical_removed_only": len(removed_only),
        "current_not_seen_in_period": len(current_not_seen),
        "removed_price_available": len(price_ok),
        "removed_price_missing": price_missing,
        "removed_only_tickers": removed_only,
        "current_not_seen_tickers": current_not_seen,
        "annual_audit": annual_df.to_dict("records"),
        "limitations": [
            "Historical membership source is third-party and is audited against expected ~500-member snapshot sizes.",
            "Ticker renames can appear as separate historical symbols; dots are normalised to Yahoo hyphens.",
            "This audit checks membership and prices only; historical fundamentals/sectors for removed names are the next stage.",
            "No production code or live SIDI rule is changed by this script.",
        ],
    }
    Path("sidi_sp500_historical_universe_audit.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("\nANNUAL SNAPSHOT AUDIT")
    print(annual_df.to_string(index=False))
    print("\nSaved historical membership audit outputs.")


if __name__ == "__main__":
    main()
