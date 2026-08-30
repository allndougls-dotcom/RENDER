"""
Diagnóstico: muestra exactamente qué devuelve yFinance
para los campos que usa el scoring fundamental.
"""
import yfinance as yf
import pandas as pd

tickers = ['AAPL', 'MSFT', 'UNH']

campos = [
    'trailingPE', 'forwardPE', 'priceToBook',
    'returnOnEquity', 'returnOnAssets',
    'profitMargins', 'grossMargins',
    'revenueGrowth', 'earningsGrowth',
    'debtToEquity', 'currentRatio',
    'freeCashflow', 'netIncomeToCommon',
    'marketCap', 'beta',
]

for ticker in tickers:
    print(f"\n{'='*50}")
    print(f"  {ticker}")
    print(f"{'='*50}")
    info = yf.Ticker(ticker).info
    for campo in campos:
        val = info.get(campo, 'N/A')
        print(f"  {campo:<25} = {val}")
