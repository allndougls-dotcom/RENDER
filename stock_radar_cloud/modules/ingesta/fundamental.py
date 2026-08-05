"""
Descarga fundamentales desde yFinance.
FMP API desactivada — endpoints legacy no disponibles en plan gratuito 2025.
"""

import time
import numpy as np
import pandas as pd
import yfinance as yf
from tqdm import tqdm


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
    """Descarga fundamentales desde yFinance para todos los tickers."""

    # Ignorar FMP aunque esté configurada — endpoints legacy bloqueados
    print(f"  Fuente: yFinance")

    rows = []
    for ticker in tqdm(tickers, desc="  Descargando fundamentales", unit="ticker"):
        d = _fund_yf(ticker)
        d["ticker"] = ticker
        rows.append(d)
        time.sleep(0.05)

    df = pd.DataFrame(rows)
    print(f"  OK: {len(df)} empresas")
    return df
