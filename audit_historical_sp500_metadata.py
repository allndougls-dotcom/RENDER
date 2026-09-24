"""Audit CIK/name/sector metadata coverage for SIDI's historical S&P 500 universe.

Uses lawcal/sp500-components-history as a secondary metadata source because it
tracks symbol, CIK, company name, sector and membership intervals. This does not
replace the primary membership snapshots yet; it enriches historical names so
SEC PIT fundamentals can later be keyed by CIK.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

import pandas as pd

from historical_ticker_aliases import clean_symbol

LAW_COMMIT = "2e59b86998a119d68e377f9f98aa7a816cfc7d5b"
LAW_URL = (
    "https://raw.githubusercontent.com/lawcal/sp500-components-history/"
    f"{LAW_COMMIT}/data/components_history.csv"
)
HANS_COMMIT = "a91ef88fad5ace83bed1f3452f451247295bcd18"
HANS_URL = (
    "https://raw.githubusercontent.com/hanshof/sp500_constituents/"
    f"{HANS_COMMIT}/sp_500_historical_components.csv"
)
START = "2020-01-01"
END = "2024-08-10"


def clean_date(s):
    return pd.to_datetime(pd.Series(s).astype(str).str.replace("*", "", regex=False), errors="coerce")


def primary_union():
    h = pd.read_csv(HANS_URL)
    h["date"] = pd.to_datetime(h["date"], errors="coerce")
    h = h.dropna(subset=["date","tickers"]).sort_values("date").drop_duplicates("date",keep="last")
    before = h[h.date <= pd.Timestamp(START)]
    opening = before.index[-1]
    h = h.loc[opening:][h.loc[opening:,"date"] <= pd.Timestamp(END)].copy()
    u=set()
    for raw in h.tickers:
        for x in str(raw).split(","):
            x=x.strip()
            if x:
                u.add(clean_symbol(x))
    return u


def main():
    print("="*116)
    print("SIDI — HISTORICAL S&P 500 CIK / SECTOR METADATA AUDIT")
    print("="*116)
    universe=primary_union()
    df=pd.read_csv(LAW_URL,dtype={"cik":str})
    df["symbol_raw"]=df["symbol"].astype(str).str.strip().str.upper().str.replace(".","-",regex=False)
    df["symbol_canonical"]=df["symbol_raw"].map(clean_symbol)
    df["date_added_clean"]=clean_date(df["date_added"])
    df["date_removed_clean"]=clean_date(df["date_removed"])
    df["cik"]=df["cik"].astype(str).str.replace(".0","",regex=False).str.zfill(10)

    # Keep rows that overlap the audit period; missing dates are open-ended.
    overlap=(df.date_added_clean.fillna(pd.Timestamp.min) <= pd.Timestamp(END)) & \
            (df.date_removed_clean.fillna(pd.Timestamp.max) >= pd.Timestamp(START))
    hist=df[overlap & df.symbol_canonical.isin(universe)].copy()

    # For each canonical symbol prefer rows with a CIK and the most recent relevant row.
    hist["has_cik"]=hist.cik.notna() & (hist.cik != "0000000nan")
    hist=hist.sort_values(["symbol_canonical","has_cik","date_added_clean"],ascending=[True,False,False])
    best=hist.drop_duplicates("symbol_canonical",keep="first")
    matched=set(best.symbol_canonical)
    missing=sorted(universe-matched)
    cik_ok=best[best.has_cik]
    sector_ok=best[best.sector.notna() & (best.sector.astype(str).str.len()>0)]

    out=best[["symbol_canonical","symbol_raw","cik","name","sector","date_added","date_removed"]].rename(columns={"symbol_canonical":"ticker"})
    out.to_csv("sidi_sp500_historical_metadata_2020_2024.csv",index=False)
    pd.DataFrame({"ticker":missing}).to_csv("sidi_sp500_historical_metadata_missing.csv",index=False)

    stats={
        "generated_at":datetime.utcnow().isoformat()+"Z",
        "period":[START,END],
        "primary_membership_source":{"repo":"hanshof/sp500_constituents","commit":HANS_COMMIT},
        "metadata_source":{"repo":"lawcal/sp500-components-history","commit":LAW_COMMIT},
        "canonical_universe":len(universe),
        "metadata_matched":len(matched),
        "metadata_match_pct":100*len(matched)/len(universe) if universe else 0,
        "cik_available":len(cik_ok),
        "cik_pct":100*len(cik_ok)/len(universe) if universe else 0,
        "sector_available":len(sector_ok),
        "sector_pct":100*len(sector_ok)/len(universe) if universe else 0,
        "missing_count":len(missing),
        "missing":missing,
    }
    Path("sidi_sp500_historical_metadata_audit.json").write_text(json.dumps(stats,indent=2),encoding="utf-8")
    print(json.dumps(stats,indent=2))

if __name__=="__main__":
    main()
