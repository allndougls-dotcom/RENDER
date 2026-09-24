"""Re-audit historical S&P 500 survivorship coverage after conservative aliases.

This is a data-quality step only. It compares the raw historical membership
universe with a canonicalized universe where only documented same-company
ticker/name changes are collapsed. Acquisitions/mergers into different issuers
remain separate and are NOT mapped.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

import pandas as pd

import backtest as bt
from historical_ticker_aliases import SAME_COMPANY_ALIASES, ANNOTATION_ALIASES, clean_symbol

SOURCE_COMMIT = "a91ef88fad5ace83bed1f3452f451247295bcd18"
SOURCE_URL = (
    "https://raw.githubusercontent.com/hanshof/sp500_constituents/"
    f"{SOURCE_COMMIT}/sp_500_historical_components.csv"
)
START = "2020-01-01"
END = "2024-08-10"
PRICE_START = "2019-09-01"
PRICE_END = "2024-08-11"


def parse_snapshot(raw: str, canonical: bool) -> set[str]:
    if raw is None or pd.isna(raw):
        return set()
    vals = [str(x).strip().upper().replace(".", "-") for x in str(raw).split(",") if str(x).strip()]
    if canonical:
        return {clean_symbol(x) for x in vals}
    return set(vals)


def load_period():
    df = pd.read_csv(SOURCE_URL)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "tickers"]).sort_values("date").drop_duplicates("date", keep="last")
    start_ts, end_ts = pd.Timestamp(START), pd.Timestamp(END)
    before = df[df.date <= start_ts]
    if before.empty:
        raise ValueError("No opening membership snapshot")
    opening = before.index[-1]
    return df.loc[opening:][df.loc[opening:, "date"] <= end_ts].copy()


def main():
    print("="*116)
    print("SIDI — HISTORICAL S&P 500 ALIAS NORMALIZATION AUDIT")
    print("="*116)
    rows = load_period()
    rows["raw_members"] = rows.tickers.map(lambda x: parse_snapshot(x, False))
    rows["canonical_members"] = rows.tickers.map(lambda x: parse_snapshot(x, True))

    raw_union = set().union(*rows.raw_members.tolist())
    canonical_union = set().union(*rows.canonical_members.tolist())
    current = {clean_symbol(t) for t in bt.load_tickers()}

    raw_removed = raw_union - current
    canonical_removed = canonical_union - current
    aliases_hit = sorted({x for x in raw_union if x in SAME_COMPANY_ALIASES})
    annotations_hit = sorted({x for x in raw_union if x in ANNOTATION_ALIASES})

    print(f"Raw historical union:       {len(raw_union)}")
    print(f"Canonical historical union: {len(canonical_union)}")
    print(f"Current canonical universe: {len(current)}")
    print(f"Raw not-current count:      {len(raw_removed)}")
    print(f"Canonical not-current:      {len(canonical_removed)}")
    print(f"Alias symbols encountered:  {len(aliases_hit)} -> {', '.join(aliases_hit)}")
    print(f"Annotations cleaned:        {len(annotations_hit)} -> {', '.join(annotations_hit)}")

    # Only re-check the canonical removed universe. These are the names that still
    # require historical prices after safe ticker continuity normalization.
    removed = sorted(canonical_removed)
    prices = bt.download_prices(removed, start_date=PRICE_START, end_date=PRICE_END) if removed else {}
    ok = {
        t for t, df in prices.items()
        if df is not None and not df.empty and pd.to_datetime(df["Date"]).max() >= pd.Timestamp(START)
    }
    missing = sorted(set(removed) - ok)

    # Track exactly what aliases collapsed and whether their canonical Yahoo series works.
    alias_rows = []
    for old, new in sorted(SAME_COMPANY_ALIASES.items()):
        if old not in raw_union:
            continue
        df = prices.get(new)
        # If canonical target is a current name it may not have been in the removed download.
        if new in current:
            single = bt.download_prices([new], start_date=PRICE_START, end_date=PRICE_END)
            df = single.get(new)
        available = bool(df is not None and not df.empty and pd.to_datetime(df["Date"]).max() >= pd.Timestamp(START))
        alias_rows.append({"historical_ticker": old, "canonical_ticker": new, "price_available": available})

    alias_df = pd.DataFrame(alias_rows)
    missing_df = pd.DataFrame({"ticker": missing})
    missing_df["category"] = "needs_delisted_or_alternate_price_source"

    annual = []
    for date in ["2020-01-02", "2021-01-04", "2022-01-03", "2023-01-03", "2024-01-02", END]:
        sub = rows[rows.date <= pd.Timestamp(date)]
        mem = set(sub.iloc[-1].canonical_members) if len(sub) else set()
        annual.append({
            "date": date,
            "members": len(mem),
            "not_current_today": len(mem-current),
            "still_current_today": len(mem&current),
        })
    annual_df = pd.DataFrame(annual)

    alias_df.to_csv("sidi_sp500_aliases_verified.csv", index=False)
    missing_df.to_csv("sidi_sp500_after_alias_missing_prices.csv", index=False)
    annual_df.to_csv("sidi_sp500_after_alias_annual_audit.csv", index=False)

    payload = {
        "generated_at": datetime.utcnow().isoformat()+"Z",
        "period": [START, END],
        "source_commit": SOURCE_COMMIT,
        "raw_historical_union": len(raw_union),
        "canonical_historical_union": len(canonical_union),
        "current_canonical_universe": len(current),
        "raw_not_current": len(raw_removed),
        "canonical_not_current": len(canonical_removed),
        "alias_symbols_encountered": aliases_hit,
        "annotation_symbols_encountered": annotations_hit,
        "canonical_removed_price_available": len(ok),
        "canonical_removed_price_missing": len(missing),
        "missing_tickers": missing,
        "annual_audit": annual,
        "method": "Only documented same-company ticker/name changes are canonicalized; acquisitions into another issuer remain separate.",
    }
    Path("sidi_sp500_alias_normalization_audit.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"\nCanonical removed names with Yahoo prices: {len(ok)}/{len(removed)}")
    print(f"Still missing after safe aliases: {len(missing)}")
    if missing:
        print(", ".join(missing))
    print("\nANNUAL CANONICAL MEMBERSHIP")
    print(annual_df.to_string(index=False))
    print("\nALIAS PRICE CHECK")
    print(alias_df.to_string(index=False) if len(alias_df) else "No aliases encountered")


if __name__ == "__main__":
    main()
