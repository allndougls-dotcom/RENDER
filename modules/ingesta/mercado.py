"""
Descarga el estado actual del mercado (SPY) y calcula
el régimen de mercado para contextualizar las señales.

Régimenes:
- ALCISTA FUERTE  : Precio > MA50 > MA200, tendencia sana
- ALCISTA         : Precio > MA200, corrección moderada
- LATERAL         : Precio cerca de MA200 (±3%), sin tendencia clara
- CORRECCIÓN      : Precio entre -5% y -15% bajo MA200
- BAJISTA         : Precio > -15% bajo MA200

AMPLIADO: además del snapshot puntual, se exporta ahora un histórico de
los últimos HISTORY_DAYS días (precio de cierre + MA200 día a día) para
poder pintar un gráfico de evolución en la app — antes solo se exportaba
el valor del día, sin contexto histórico visual.
"""

import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta

HISTORY_DAYS = 250  # ~1 año de sesiones bursátiles, suficiente para ver todo el tramo con MA200 ya formada


def get_market_context() -> dict:
    """Descarga SPY + VIX y calcula el régimen de mercado actual + histórico."""
    try:
        end   = datetime.today().strftime('%Y-%m-%d')
        # Se pide bastante más histórico del que se exporta (250d) para que
        # la MA200 del PRIMER día exportado ya esté bien formada (necesita
        # 200 días previos de datos para calcularse), no truncada/NaN.
        start = (datetime.today() - timedelta(days=365 * 2 + 60)).strftime('%Y-%m-%d')

        spy = yf.download('SPY', start=start, end=end,
                          auto_adjust=True, progress=False)

        if spy.empty or len(spy) < 50:
            return _default_context()

        closes = spy['Close'].squeeze()
        n      = len(closes)

        price  = float(closes.iloc[-1])
        ma50   = float(closes.rolling(50).mean().iloc[-1])
        ma200  = float(closes.rolling(200).mean().iloc[-1]) if n >= 200 else float(closes.rolling(n).mean().iloc[-1])
        ma20   = float(closes.rolling(20).mean().iloc[-1])

        # RSI del índice
        delta  = closes.diff()
        gain   = delta.clip(lower=0).rolling(14).mean()
        loss   = (-delta.clip(upper=0)).rolling(14).mean()
        rs     = gain / loss.replace(0, np.nan)
        rsi_series = (100 - 100 / (1 + rs)).round(2)
        rsi    = float(rsi_series.iloc[-1])

        # Drawdown desde máximo de 52 semanas
        high52 = float(closes.iloc[-252:].max()) if n >= 252 else float(closes.max())
        dd52   = (price / high52 - 1) * 100

        # Distancia a MA200
        vs200  = (price / ma200 - 1) * 100

        # Pendiente MA200 (20 días)
        ma200_20d_ago = float(closes.rolling(200).mean().iloc[-21]) if n >= 221 else ma200
        ma200_slope   = (ma200 - ma200_20d_ago) / ma200_20d_ago * 100

        # Volatilidad reciente (std 20d vs std 60d)
        vol20  = float(closes.pct_change().rolling(20).std().iloc[-1]) * 100
        vol60  = float(closes.pct_change().rolling(60).std().iloc[-1]) * 100
        vol_ratio = vol20 / vol60 if vol60 > 0 else 1.0

        # ── Histórico para el gráfico (precio + MA200 + RSI, últimos HISTORY_DAYS) ──
        ma200_series = closes.rolling(200).mean()
        history_dates  = spy.index[-HISTORY_DAYS:]
        history_close  = closes.iloc[-HISTORY_DAYS:]
        history_ma200  = ma200_series.iloc[-HISTORY_DAYS:]
        history_rsi    = rsi_series.iloc[-HISTORY_DAYS:]

        history = []
        for d, c, m, r in zip(history_dates, history_close, history_ma200, history_rsi):
            history.append({
                'date':  d.strftime('%Y-%m-%d'),
                'close': round(float(c), 2),
                'ma200': round(float(m), 2) if not pd.isna(m) else None,
                'rsi':   round(float(r), 1) if not pd.isna(r) else None,
            })

        # ── VIX (índice de volatilidad) ────────────────────────────
        # Se descarga aparte porque un fallo aquí no debe tumbar todo
        # el contexto de mercado (SPY es el dato crítico, VIX es un extra).
        vix_value = None
        try:
            vix_data = yf.download('^VIX', start=start, end=end,
                                   auto_adjust=True, progress=False)
            if not vix_data.empty:
                vix_close = vix_data['Close'].squeeze()
                vix_value = round(float(vix_close.iloc[-1]), 2)
        except Exception as e:
            print(f"  ⚠ Error obteniendo VIX (no crítico, se deja en None): {e}")

        # ── RÉGIMEN ──────────────────────────────────────────────
        if price > ma50 and ma50 > ma200 and vs200 > 3 and ma200_slope > 0:
            regime = 'ALCISTA FUERTE'
            regime_color = '#22c55e'
            regime_icon  = '🟢'
            regime_desc  = 'Tendencia alcista sana. Condiciones favorables para mean reversion.'
            regime_score = 10

        elif price > ma200 and vs200 > -2:
            regime = 'ALCISTA'
            regime_color = '#86efac'
            regime_icon  = '🟩'
            regime_desc  = 'Por encima de MA200. Rebotes técnicos tienen alta probabilidad.'
            regime_score = 8

        elif abs(vs200) <= 3:
            regime = 'LATERAL'
            regime_color = '#fbbf24'
            regime_icon  = '🟡'
            regime_desc  = 'Mercado en zona de indecisión. Rebotes posibles pero menos predecibles.'
            regime_score = 5

        elif vs200 > -15:
            regime = 'CORRECCIÓN'
            regime_color = '#f97316'
            regime_icon  = '🟠'
            regime_desc  = 'Corrección activa. Rebotes individuales más difíciles. Reduce tamaño de posición.'
            regime_score = 3

        else:
            regime = 'BAJISTA'
            regime_color = '#ef4444'
            regime_icon  = '🔴'
            regime_desc  = 'Tendencia bajista. Rebotes son traps. Considera no operar hasta recuperar MA200.'
            regime_score = 1

        # ── AJUSTE DE FILTROS RECOMENDADO ─────────────────────────
        if regime_score >= 8:
            filter_rec = 'Filtros estándar (Score ≥6.5, DD ≥10%)'
        elif regime_score == 5:
            filter_rec = 'Sube umbral (Score ≥7.0, DD ≥12%)'
        elif regime_score == 3:
            filter_rec = 'Muy selectivo (Score ≥7.5, DD ≥15%), tamaño 50%'
        else:
            filter_rec = 'No operar o solo posiciones muy pequeñas'

        return {
            'date':          datetime.today().strftime('%Y-%m-%d %H:%M'),
            'spy_price':     round(price, 2),
            'spy_ma50':      round(ma50, 2),
            'spy_ma200':     round(ma200, 2),
            'spy_rsi':       round(rsi, 1),
            'spy_vs200':     round(vs200, 2),
            'spy_dd52':      round(dd52, 2),
            'spy_ma200_slope': round(ma200_slope, 3),
            'spy_vol_ratio': round(vol_ratio, 2),
            'vix':           vix_value,
            'market_regime': regime,
            'regime_color':  regime_color,
            'regime_icon':   regime_icon,
            'regime_desc':   regime_desc,
            'regime_score':  regime_score,
            'filter_rec':    filter_rec,
            'history':       history,
        }

    except Exception as e:
        print(f"  ⚠ Error obteniendo contexto de mercado: {e}")
        return _default_context()


def _default_context() -> dict:
    return {
        'date':            datetime.today().strftime('%Y-%m-%d %H:%M'),
        'spy_price':       0,
        'spy_ma50':        0,
        'spy_ma200':       0,
        'spy_rsi':         50,
        'spy_vs200':       0,
        'spy_dd52':        0,
        'spy_ma200_slope': 0,
        'spy_vol_ratio':   1,
        'vix':             None,
        'market_regime':   'DESCONOCIDO',
        'regime_color':    '#6670a0',
        'regime_icon':     '⚪',
        'regime_desc':     'No se pudo obtener el contexto de mercado.',
        'regime_score':    5,
        'filter_rec':      'Filtros estándar',
        'history':         [],
    }


def calcular_mercado() -> dict:
    print("  ⏳ Descargando contexto de mercado (SPY + VIX)...")
    ctx = get_market_context()
    print(f"  ✅ Mercado: {ctx['regime_icon']} {ctx['market_regime']}")
    vix_str = f"{ctx['vix']}" if ctx['vix'] is not None else "N/D"
    print(f"     SPY: ${ctx['spy_price']} | vs MA200: {ctx['spy_vs200']:+.1f}% | RSI: {ctx['spy_rsi']} | VIX: {vix_str}")
    print(f"     Recomendación: {ctx['filter_rec']}")
    return ctx
