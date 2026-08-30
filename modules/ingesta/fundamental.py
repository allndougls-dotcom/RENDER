"""Descarga fundamentales desde FMP API o yFinance como fallback."""

import time
import numpy as np
import pandas as pd
import requests
import yfinance as yf
from tqdm import tqdm


BASE_FMP = "https://financialmodelingprep.com/api/v3"


def _fmp_get(endpoint: str, api_key: str, params: dict = None):
    p = params or {}
    p["apikey"] = api_key
    try:
        r = requests.get(f"{BASE_FMP}/{endpoint}", params=p, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _fund_fmp(ticker: str, api_key: str) -> dict:
    out = {}

    rat = _fmp_get(f"ratios-ttm/{ticker}", api_key)
    if rat and len(rat):
        r = rat[0]
        out.update({
            "pe":             r.get("peRatioTTM"),
            "pb":             r.get("priceToBookRatioTTM"),
            "ps":             r.get("priceToSalesRatioTTM"),
            "ev_ebitda":      r.get("enterpriseValueMultipleTTM"),
            "roe":            r.get("returnOnEquityTTM"),
            "roa":            r.get("returnOnAssetsTTM"),
            "profit_margin":  r.get("netProfitMarginTTM"),
            "gross_margin":   r.get("grossProfitMarginTTM"),
            "debt_equity":    r.get("debtEquityRatioTTM"),
            "current_ratio":  r.get("currentRatioTTM"),
            "fcf_yield":      r.get("freeCashFlowYieldTTM"),
            "dividend_yield": r.get("dividendYieldTTM"),
        })

    pro = _fmp_get(f"profile/{ticker}", api_key)
    if pro and len(pro):
        out.update({"market_cap": pro[0].get("mktCap"), "beta": pro[0].get("beta")})

    inc = _fmp_get(f"income-statement/{ticker}", api_key, {"limit": 2})
    if inc and len(inc) >= 2:
        rv, rp = inc[0].get("revenue", 0), inc[1].get("revenue", 0)
        ep0, ep1 = inc[0].get("eps"), inc[1].get("eps")
        ni = sum(q.get("netIncome", 0) for q in inc[:2])
        out.update({
            "revenue_ttm":    rv,
            "revenue_growth": (rv - rp) / abs(rp) if rp else np.nan,
            "eps_ttm":        ep0,
            "eps_growth":     (ep0 - ep1) / abs(ep1) if (ep0 and ep1 and ep1 != 0) else np.nan,
            "net_income":     ni,
        })

    cf = _fmp_get(f"cash-flow-statement/{ticker}", api_key, {"limit": 1})
    if cf and len(cf):
        fcf = cf[0].get("freeCashFlow")
        ni  = out.get("net_income", 0)
        out["fcf"] = fcf
        out["fcf_ni_ratio"] = fcf / ni if (fcf and ni and ni != 0) else np.nan

    return out


def _fund_yf(ticker: str) -> dict:
    try:
        info = yf.Ticker(ticker).info
        ni   = info.get("netIncomeToCommon")
        fcf  = info.get("freeCashflow")
        return {
            "pe":             info.get("trailingPE"),
            "forward_pe":     info.get("forwardPE"),
            "pb":             info.get("priceToBook"),
            "ps":             info.get("priceToSalesTrailing12Months"),
            "ev_ebitda":      info.get("enterpriseToEbitda"),
            "roe":            info.get("returnOnEquity"),
            "roa":            info.get("returnOnAssets"),
            "profit_margin":  info.get("profitMargins"),
            "gross_margin":   info.get("grossMargins"),
            "revenue_growth": info.get("revenueGrowth"),
            "eps_growth":     info.get("earningsGrowth"),
            "debt_equity":    info.get("debtToEquity"),
            "current_ratio":  info.get("currentRatio"),
            "fcf":            fcf,
            "net_income":     ni,
            "fcf_ni_ratio":   fcf / ni if (fcf and ni and ni != 0) else np.nan,
            "market_cap":     info.get("marketCap"),
            "beta":           info.get("beta"),
            "short_ratio":    info.get("shortRatio"),
            "dividend_yield": info.get("dividendYield"),
            "shares_outstanding": info.get("sharesOutstanding"),
        }
    except Exception:
        return {}


def calcular_fundamentales(tickers: list, cfg: dict) -> pd.DataFrame:
    use_fmp = cfg["USE_FMP"]
    api_key = cfg["FMP_API_KEY"]
    rows    = []

    src = "FMP API" if use_fmp else "yFinance"
    print(f"  📡 Fuente: {src}")

    for ticker in tqdm(tickers, desc=f"  Descargando ({src})", unit="ticker"):
        d = _fund_fmp(ticker, api_key) if use_fmp else _fund_yf(ticker)
        d["ticker"] = ticker
        rows.append(d)
        time.sleep(0.25 if use_fmp else 0.05)

    df = pd.DataFrame(rows)
    print(f"  ✅ Fundamentales: {len(df)} empresas")
    return df
