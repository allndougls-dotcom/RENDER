"""
Obtiene la fecha del próximo earnings, el histórico de sorpresas EPS/Revenue
y el contexto sectorial (ETF, peers, variables macro) para cada empresa.
Se integra en el pipeline de ingesta como paso adicional.
"""

import numpy as np
import pandas as pd
import yfinance as yf
import time
from datetime import datetime, timedelta
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


def get_earnings_surprise_history(ticker: str) -> dict:
    """
    Obtiene el ultimo earnings reportado con su sorpresa de EPS/Revenue,
    usando yf.Ticker().earnings_dates (incluye pasado y futuro programado).
    Devuelve un dict con los campos del bloque 'earnings_data' del input
    ideal para el analizador de noticias. Si no hay datos, devuelve valores
    None/np.nan de forma segura (nunca lanza excepcion hacia el pipeline).
    """
    out = {
        'latest_earnings_date':  None,
        'eps_actual':            np.nan,
        'eps_estimate':          np.nan,
        'eps_surprise_pct':      np.nan,
        'revenue_actual':        np.nan,
        'revenue_estimate':      np.nan,
        'revenue_surprise_pct':  np.nan,
        'next_earnings_date':    None,
    }
    try:
        t  = yf.Ticker(ticker)
        ed = t.earnings_dates  # DataFrame indexado por fecha, incluye pasado+futuro
        if ed is None or len(ed) == 0:
            return out

        today = pd.Timestamp.today(tz=ed.index.tz) if ed.index.tz else pd.Timestamp.today()

        # ── Próximo earnings (primera fecha futura) ──────────────
        futuras = ed[ed.index > today]
        if len(futuras) > 0:
            out['next_earnings_date'] = futuras.index[-1].strftime('%Y-%m-%d')  # mas lejana=mas fiable

        # ── Último earnings YA REPORTADO (con EPS actual no nulo) ─
        pasadas = ed[ed.index <= today].copy()
        if 'Reported EPS' in pasadas.columns:
            pasadas = pasadas.dropna(subset=['Reported EPS'])
        if len(pasadas) > 0:
            ultima = pasadas.iloc[0]  # yfinance ordena descendente por fecha
            out['latest_earnings_date'] = pasadas.index[0].strftime('%Y-%m-%d')

            eps_est = ultima.get('EPS Estimate', np.nan)
            eps_act = ultima.get('Reported EPS', np.nan)
            out['eps_estimate'] = float(eps_est) if not pd.isna(eps_est) else np.nan
            out['eps_actual']   = float(eps_act) if not pd.isna(eps_act) else np.nan

            surprise_pct = ultima.get('Surprise(%)', np.nan)
            if not pd.isna(surprise_pct):
                # yfinance a veces lo da en decimal (0.09) y a veces en % (9.1) según version
                sp = float(surprise_pct)
                out['eps_surprise_pct'] = round(sp * 100, 1) if abs(sp) < 1 else round(sp, 1)
            elif not pd.isna(out['eps_estimate']) and out['eps_estimate'] != 0 and not pd.isna(out['eps_actual']):
                out['eps_surprise_pct'] = round(
                    (out['eps_actual'] - out['eps_estimate']) / abs(out['eps_estimate']) * 100, 1)

        # yfinance earnings_dates no siempre trae revenue — se deja como
        # extensión futura vía FMP (analyst-estimates) en la Fase 2.

    except Exception:
        pass

    return out


def calcular_earnings(tickers: list) -> pd.DataFrame:
    """
    Descarga fechas de earnings + histórico de sorpresas para todos los tickers.
    Devuelve DataFrame con columnas: ticker, earnings_days_next, earnings_date,
    earnings_warning, latest_earnings_date, eps_actual, eps_estimate,
    eps_surprise_pct, next_earnings_date_exact.
    """
    rows = []
    today = datetime.today().date()

    for ticker in tqdm(tickers, desc="  Descargando earnings dates", unit="ticker"):
        days = get_earnings_days(ticker)
        surprise_data = get_earnings_surprise_history(ticker)

        # Calcular fecha aproximada (fallback si next_earnings_date exacta no vino)
        if days != 999:
            earnings_date = (today + timedelta(days=days)).isoformat()
        else:
            earnings_date = None

        rows.append({
            'ticker':                ticker,
            'earnings_days_next':    days,
            'earnings_date':         surprise_data['next_earnings_date'] or earnings_date,
            'earnings_warning':      days <= 7 and days >= -2,  # dentro de ventana de riesgo
            'latest_earnings_date':  surprise_data['latest_earnings_date'],
            'eps_actual':            surprise_data['eps_actual'],
            'eps_estimate':          surprise_data['eps_estimate'],
            'eps_surprise_pct':      surprise_data['eps_surprise_pct'],
        })

        time.sleep(0.1)  # rate limiting suave

    df = pd.DataFrame(rows)

    # Resumen
    con_datos    = (df['earnings_days_next'] != 999).sum()
    en_riesgo    = df['earnings_warning'].sum()
    proximos_7d  = ((df['earnings_days_next'] >= 0) & (df['earnings_days_next'] <= 7)).sum()
    con_sorpresa = df['eps_surprise_pct'].notna().sum()

    print(f"  ✅ Earnings dates: {con_datos}/{len(df)} con datos")
    print(f"     En ventana de riesgo (±7d): {en_riesgo}")
    print(f"     Próximos 7 días: {proximos_7d}")
    print(f"     Con sorpresa EPS histórica: {con_sorpresa}/{len(df)}")

    return df
