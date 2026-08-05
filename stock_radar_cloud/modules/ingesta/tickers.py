"""Obtiene los componentes del S&P500 desde Wikipedia."""

import io
import time
import requests
import pandas as pd


def get_sp500_tickers(max_n: int = 503) -> pd.DataFrame:
    print(f"  Scraping Wikipedia...")

    url     = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    for intento in range(3):
        try:
            timeout = 15 + intento * 15
            if intento > 0:
                print(f"  Reintentando (intento {intento+1}/3, timeout {timeout}s)...")
                time.sleep(3)
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            break
        except requests.exceptions.Timeout:
            if intento == 2:
                print("  Wikipedia no responde. Usando lista de respaldo...")
                return _fallback_tickers(max_n)
            continue
        except Exception as e:
            if intento == 2:
                print(f"  Error: {e}. Usando lista de respaldo...")
                return _fallback_tickers(max_n)
            continue

    tables = pd.read_html(io.StringIO(r.text))
    df     = tables[0]
    df.columns = [c.strip() for c in df.columns]

    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if "symbol" in cl or "ticker" in cl:         col_map[c] = "ticker"
        elif "security" in cl or "company" in cl:    col_map[c] = "name"
        elif "gics sector" in cl or ("sector" in cl and "sub" not in cl): col_map[c] = "sector"
        elif "sub" in cl and "industry" in cl:       col_map[c] = "industry"
        elif "date" in cl and "added" in cl:         col_map[c] = "date_added"

    df = df.rename(columns=col_map)
    df["ticker"] = df["ticker"].str.replace(".", "-", regex=False)
    keep = [c for c in ["ticker","name","sector","industry","date_added"] if c in df.columns]
    df   = df[keep].head(max_n).reset_index(drop=True)

    print(f"  OK: {len(df)} empresas · {df['sector'].nunique()} sectores")
    return df


def _fallback_tickers(max_n: int) -> pd.DataFrame:
    data = [
        ("AAPL","Apple Inc.","Information Technology"),
        ("MSFT","Microsoft Corp.","Information Technology"),
        ("GOOGL","Alphabet Inc.","Communication Services"),
        ("AMZN","Amazon.com Inc.","Consumer Discretionary"),
        ("NVDA","NVIDIA Corp.","Information Technology"),
        ("META","Meta Platforms","Communication Services"),
        ("BRK-B","Berkshire Hathaway","Financials"),
        ("LLY","Eli Lilly","Health Care"),
        ("JPM","JPMorgan Chase","Financials"),
        ("V","Visa Inc.","Financials"),
        ("XOM","Exxon Mobil","Energy"),
        ("UNH","UnitedHealth Group","Health Care"),
        ("JNJ","Johnson & Johnson","Health Care"),
        ("PG","Procter & Gamble","Consumer Staples"),
        ("MA","Mastercard Inc.","Financials"),
        ("HD","Home Depot","Consumer Discretionary"),
        ("CVX","Chevron Corp.","Energy"),
        ("MRK","Merck & Co.","Health Care"),
        ("ABBV","AbbVie Inc.","Health Care"),
        ("COST","Costco Wholesale","Consumer Staples"),
        ("PEP","PepsiCo Inc.","Consumer Staples"),
        ("KO","Coca-Cola Co.","Consumer Staples"),
        ("ADBE","Adobe Inc.","Information Technology"),
        ("CRM","Salesforce Inc.","Information Technology"),
        ("NFLX","Netflix Inc.","Communication Services"),
        ("AMD","Advanced Micro Devices","Information Technology"),
        ("TMO","Thermo Fisher","Health Care"),
        ("ACN","Accenture PLC","Information Technology"),
        ("AVGO","Broadcom Inc.","Information Technology"),
        ("MCD","McDonald's Corp.","Consumer Discretionary"),
        ("WMT","Walmart Inc.","Consumer Staples"),
        ("BAC","Bank of America","Financials"),
        ("LIN","Linde PLC","Materials"),
        ("DIS","Walt Disney Co.","Communication Services"),
        ("PM","Philip Morris","Consumer Staples"),
        ("TXN","Texas Instruments","Information Technology"),
        ("ORCL","Oracle Corp.","Information Technology"),
        ("CAT","Caterpillar Inc.","Industrials"),
        ("GS","Goldman Sachs","Financials"),
        ("SPGI","S&P Global Inc.","Financials"),
        ("RTX","RTX Corp.","Industrials"),
        ("NEE","NextEra Energy","Utilities"),
        ("HON","Honeywell Intl.","Industrials"),
        ("ISRG","Intuitive Surgical","Health Care"),
        ("AMAT","Applied Materials","Information Technology"),
        ("LRCX","Lam Research","Information Technology"),
        ("DE","Deere & Company","Industrials"),
        ("AMGN","Amgen Inc.","Health Care"),
        ("AXP","American Express","Financials"),
        ("BKNG","Booking Holdings","Consumer Discretionary"),
    ]
    df = pd.DataFrame(data[:max_n], columns=["ticker","name","sector"])
    df["industry"] = ""
    df["date_added"] = ""
    print(f"  Lista de respaldo: {len(df)} empresas")
    return df
