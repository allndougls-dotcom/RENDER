"""SEC EDGAR point-in-time fundamental reconstruction (v1).

This module intentionally prioritises *information timing* over perfect parity
with the current yFinance/FMP score. Every accounting fact must satisfy
`filed <= asof_date`; future filings are never used.

v1 methodology
--------------
- Growth / NI / FCF: latest two annual 10-K facts known by as-of date.
- Balance-sheet ratios: latest 10-Q/10-K instant facts known by as-of date.
- Historical valuation: yFinance close on/before as-of date x SEC shares.
- Output metrics use the same broad units expected by SIDI scoring.py.

This is a validation score, not yet a drop-in replacement for production's
current TTM score. A later v2 can reconstruct quarterly TTM flows.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import time
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import requests
import yfinance as yf

SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_HEADERS = {
    "User-Agent": "SIDI-backtest/1.0 (github.com/allndougls-dotcom/RENDER)",
    "Accept-Encoding": "gzip, deflate",
}
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A"}
ANNUAL_FORMS = {"10-K", "10-K/A"}

TAG_CANDIDATES = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues", "SalesRevenueNet",
    ],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "operating_cf": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForAdditionsToPropertyPlantAndEquipment",
    ],
    "debt_current": [
        "LongTermDebtCurrent",
        "LongTermDebtAndFinanceLeaseObligationsCurrent",
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
    ],
    "debt_noncurrent": [
        "LongTermDebtNoncurrent",
        "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
        "LongTermDebtAndCapitalLeaseObligationsNoncurrent",
    ],
    "short_debt": ["ShortTermBorrowings", "ShortTermDebtCurrent"],
    "shares": ["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"],
}


def sec_get_json(url: str, session: requests.Session | None = None) -> dict:
    s = session or requests.Session()
    r = s.get(url, headers=SEC_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def ticker_cik_map(session: requests.Session | None = None) -> dict[str, int]:
    raw = sec_get_json(SEC_TICKERS, session)
    out = {}
    for item in raw.values():
        ticker = str(item.get("ticker", "")).upper().replace(".", "-")
        if ticker:
            out[ticker] = int(item["cik_str"])
    return out


def _concept_units(companyfacts: dict, candidates: Iterable[str]) -> list[dict]:
    usgaap = companyfacts.get("facts", {}).get("us-gaap", {})
    dei = companyfacts.get("facts", {}).get("dei", {})
    for tag in candidates:
        concept = usgaap.get(tag) or dei.get(tag)
        if not concept:
            continue
        units = concept.get("units", {})
        # Prefer normal financial units, but accept the first available unit.
        for key in ("USD", "USD/shares", "shares", "pure"):
            if key in units:
                return units[key]
        if units:
            return next(iter(units.values()))
    return []


def _valid_fact(f: dict, asof: str, annual_only: bool = False) -> bool:
    filed = str(f.get("filed", ""))
    end = str(f.get("end", ""))
    form = str(f.get("form", ""))
    if not filed or not end or filed > asof or end > asof:
        return False
    if annual_only:
        return form in ANNUAL_FORMS
    return form in FORMS


def latest_instant(companyfacts: dict, candidates: list[str], asof: str) -> float:
    facts = [f for f in _concept_units(companyfacts, candidates) if _valid_fact(f, asof)]
    if not facts:
        return np.nan
    # Deduplicate same period/end; latest filing known at as-of wins.
    facts.sort(key=lambda f: (str(f.get("end", "")), str(f.get("filed", ""))))
    return float(facts[-1].get("val", np.nan))


def annual_series(companyfacts: dict, candidates: list[str], asof: str) -> list[dict]:
    facts = [f for f in _concept_units(companyfacts, candidates) if _valid_fact(f, asof, annual_only=True)]
    clean = []
    for f in facts:
        start, end = f.get("start"), f.get("end")
        if not start or not end:
            continue
        try:
            days = (pd.Timestamp(end) - pd.Timestamp(start)).days
        except Exception:
            continue
        # Filter out quarterly/YTD contexts that happen to be repeated in a 10-K.
        if not 300 <= days <= 430:
            continue
        clean.append(f)
    # For each period end, keep latest filing available as-of.
    by_end: dict[str, dict] = {}
    for f in clean:
        end = str(f["end"])
        prev = by_end.get(end)
        if prev is None or str(f.get("filed", "")) > str(prev.get("filed", "")):
            by_end[end] = f
    return [by_end[k] for k in sorted(by_end)]


def latest_two_annual(companyfacts: dict, candidates: list[str], asof: str) -> tuple[float, float]:
    arr = annual_series(companyfacts, candidates, asof)
    if not arr:
        return np.nan, np.nan
    latest = float(arr[-1].get("val", np.nan))
    prior = float(arr[-2].get("val", np.nan)) if len(arr) >= 2 else np.nan
    return latest, prior


def historical_close(ticker: str, asof: str) -> float:
    d = pd.Timestamp(asof)
    start = (d - pd.Timedelta(days=10)).date().isoformat()
    end = (d + pd.Timedelta(days=1)).date().isoformat()
    try:
        hist = yf.download(ticker.replace("-", "."), start=start, end=end, progress=False, auto_adjust=True)
        if hist.empty:
            return np.nan
        s = hist["Close"]
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        s = s.dropna()
        return float(s.iloc[-1]) if len(s) else np.nan
    except Exception:
        return np.nan


def safe_div(a: float, b: float, scale: float = 1.0) -> float:
    if pd.isna(a) or pd.isna(b) or b == 0:
        return np.nan
    return float(a / b * scale)


def reconstruct_one(ticker: str, cik: int, asof: str, sector: str | None = None,
                    session: requests.Session | None = None) -> dict[str, Any]:
    facts = sec_get_json(SEC_FACTS.format(cik=cik), session)

    revenue, revenue_prev = latest_two_annual(facts, TAG_CANDIDATES["revenue"], asof)
    ni, ni_prev = latest_two_annual(facts, TAG_CANDIDATES["net_income"], asof)
    eps, eps_prev = latest_two_annual(facts, TAG_CANDIDATES["eps"], asof)
    ocf, _ = latest_two_annual(facts, TAG_CANDIDATES["operating_cf"], asof)
    capex, _ = latest_two_annual(facts, TAG_CANDIDATES["capex"], asof)

    equity = latest_instant(facts, TAG_CANDIDATES["equity"], asof)
    ca = latest_instant(facts, TAG_CANDIDATES["current_assets"], asof)
    cl = latest_instant(facts, TAG_CANDIDATES["current_liabilities"], asof)
    debt_c = latest_instant(facts, TAG_CANDIDATES["debt_current"], asof)
    debt_n = latest_instant(facts, TAG_CANDIDATES["debt_noncurrent"], asof)
    short_d = latest_instant(facts, TAG_CANDIDATES["short_debt"], asof)
    shares = latest_instant(facts, TAG_CANDIDATES["shares"], asof)

    debt_parts = [x for x in (debt_c, debt_n, short_d) if not pd.isna(x)]
    total_debt = sum(debt_parts) if debt_parts else np.nan
    fcf = (ocf - abs(capex)) if not pd.isna(ocf) and not pd.isna(capex) else np.nan

    price = historical_close(ticker, asof)
    market_cap = price * shares if not pd.isna(price) and not pd.isna(shares) else np.nan

    revenue_growth = safe_div(revenue - revenue_prev, abs(revenue_prev)) if not pd.isna(revenue) and not pd.isna(revenue_prev) else np.nan
    eps_growth = safe_div(eps - eps_prev, abs(eps_prev)) if not pd.isna(eps) and not pd.isna(eps_prev) and eps_prev != 0 else np.nan
    roe = safe_div(ni, equity)
    debt_equity = safe_div(total_debt, equity, 100.0)
    current_ratio = safe_div(ca, cl)
    fcf_ni_ratio = safe_div(fcf, ni)
    pe = safe_div(price, eps)
    pb = safe_div(market_cap, equity)

    return {
        "asof_date": asof,
        "ticker": ticker,
        "cik": cik,
        "sector": sector,
        "price": price,
        "revenue_growth": revenue_growth,
        "eps_growth": eps_growth,
        "roe": roe,
        "debt_equity": debt_equity,
        "current_ratio": current_ratio,
        "fcf_ni_ratio": fcf_ni_ratio,
        "pe": pe,
        "pb": pb,
        "fcf": fcf,
        "market_cap": market_cap,
        "revenue": revenue,
        "net_income": ni,
        "eps": eps,
        "equity": equity,
        "shares": shares,
        "sec_entity": facts.get("entityName"),
    }


def load_sector_map() -> dict[str, str]:
    files = sorted(Path("data/master").glob("sp500_full_export_*.csv"))
    if not files:
        return {}
    df = pd.read_csv(files[-1], usecols=lambda c: c in {"ticker", "sector"})
    return {str(r.ticker).upper().replace(".", "-"): str(r.sector) for r in df.itertuples()}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--asof", required=True, help="YYYY-MM-DD; future filings are excluded")
    p.add_argument("--tickers", default="AAPL,MSFT,NVDA,JPM,XOM,JNJ,PG,CAT")
    p.add_argument("--out", default="sec_pit_smoke.csv")
    p.add_argument("--sleep", type=float, default=0.15)
    args = p.parse_args()

    pd.Timestamp(args.asof)  # validate date
    tickers = [x.strip().upper().replace(".", "-") for x in args.tickers.split(",") if x.strip()]
    session = requests.Session()
    cikmap = ticker_cik_map(session)
    sectors = load_sector_map()

    rows = []
    for ticker in tickers:
        cik = cikmap.get(ticker)
        if cik is None:
            print(f"MISS CIK {ticker}")
            continue
        try:
            row = reconstruct_one(ticker, cik, args.asof, sectors.get(ticker), session)
            rows.append(row)
            present = sum(not pd.isna(row.get(k)) for k in ["revenue_growth","eps_growth","roe","debt_equity","current_ratio","fcf_ni_ratio","pe","pb"])
            print(f"OK {ticker:<5} CIK={cik:010d} metrics={present}/8 price={row['price']}")
        except Exception as exc:
            print(f"ERR {ticker}: {type(exc).__name__}: {exc}")
        time.sleep(max(args.sleep, 0.11))  # remain comfortably below SEC 10 req/s guidance

    df = pd.DataFrame(rows)
    df.to_csv(args.out, index=False)
    print(f"Saved {args.out}: {len(df)} rows")


if __name__ == "__main__":
    main()
