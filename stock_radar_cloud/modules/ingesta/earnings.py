"""
Obtiene la fecha del próximo earnings para cada empresa.
Se integra en el pipeline de ingesta como paso adicional.
"""

import numpy as np
import pandas as pd
import yfinance as yf
import time
from datetime import datetime
from tqdm import tqdm


def get_earnings_days(ticker: str) -> int:
    """
    Devuelve los días hasta el próximo earnings.
    - Negativo: earnings ya pasaron hace N días
    - Positivo: earnings en N días
    - 999: no disponible
    """
    try:
        t   = yf.Ticker(ticker)
        cal = t.calendar

        # yFinance devuelve dict con 'Earnings Date' como lista de timestamps
        if isinstance(cal, dict) and 'Earnings Date' in cal:
            dates = cal['Earnings Date']
            if not dates:
                return 999
            # Puede ser lista o valor único
            if not isinstance(dates, list):
                dates = [dates]
            today = datetime.today().date()
            # Buscar la fecha más próxima (futura o pasada reciente)
            closest = None
            for d in dates:
                try:
                    if hasattr(d, 'date'):
                        d = d.date()
                    elif isinstance(d, str):
                        d = datetime.strptime(d[:10], '%Y-%m-%d').date()
                    if closest is None or abs((d - today).days) < abs((closest - today).days):
                        closest = d
                except Exception:
                    continue
            if closest:
                return (closest - today).days
        return 999

    except Exception:
        return 999


def calcular_earnings(tickers: list) -> pd.DataFrame:
    """
    Descarga fechas de earnings para todos los tickers.
    Devuelve DataFrame con columnas: ticker, earnings_days_next, earnings_date
    """
    rows = []
    today = datetime.today().date()

    for ticker in tqdm(tickers, desc="  Descargando earnings dates", unit="ticker"):
        days = get_earnings_days(ticker)

        # Calcular fecha aproximada
        if days != 999:
            from datetime import timedelta
            earnings_date = (today + timedelta(days=days)).isoformat()
        else:
            earnings_date = None

        rows.append({
            'ticker':             ticker,
            'earnings_days_next': days,
            'earnings_date':      earnings_date,
            'earnings_warning':   days <= 7 and days >= -2,  # dentro de ventana de riesgo
        })

        time.sleep(0.1)  # rate limiting suave

    df = pd.DataFrame(rows)

    # Resumen
    con_datos   = (df['earnings_days_next'] != 999).sum()
    en_riesgo   = df['earnings_warning'].sum()
    proximos_7d = ((df['earnings_days_next'] >= 0) & (df['earnings_days_next'] <= 7)).sum()

    print(f"  ✅ Earnings dates: {con_datos}/{len(df)} con datos")
    print(f"     En ventana de riesgo (±7d): {en_riesgo}")
    print(f"     Próximos 7 días: {proximos_7d}")

    return df
