"""Wrapper for sec_pit_fundamentals with a GitHub-runner-safe CIK mapping.

SEC's www.sec.gov ticker map can return HTTP 403 from hosted CI IPs even when
companyfacts on data.sec.gov is accessible. For the S&P 500 universe we can
obtain the same public CIK identifiers from Wikipedia's constituent table,
then use SEC itself as the authoritative source for all financial facts.
"""
from __future__ import annotations

import pandas as pd

import sec_pit_fundamentals as pit

WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def robust_cik_map(session=None):
    try:
        return pit.ticker_cik_map(session)
    except Exception as exc:
        print(f"SEC ticker map unavailable ({type(exc).__name__}); using S&P 500 CIK table fallback")
        tables = pd.read_html(WIKI_SP500)
        df = tables[0]
        out = {}
        for row in df.itertuples(index=False):
            ticker = str(getattr(row, "Symbol")).upper().replace(".", "-")
            cik = int(getattr(row, "CIK"))
            out[ticker] = cik
        print(f"CIK fallback loaded: {len(out)} S&P 500 securities")
        return out


pit.ticker_cik_map = robust_cik_map

if __name__ == "__main__":
    pit.main()
