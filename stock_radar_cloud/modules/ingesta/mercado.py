"""
Descarga el estado actual del mercado (SPY + VIX) y calcula
el régimen de mercado para contextualizar las señales.

Regimenes SPY:
- ALCISTA FUERTE : Precio > MA50 > MA200, tendencia sana
- ALCISTA        : Precio > MA200
- LATERAL        : Precio ±3% de MA200
- CORRECCIÓN     : Precio -5% a -15% bajo MA200
- BAJISTA        : Precio < -15% de MA200

Niveles VIX:
- CALMO    : VIX < 20 → operar normal
- MODERADO : VIX 20-25 → reducir tamaño 25%
- ALTO     : VIX 25-30 → reducir tamaño 50%, subir umbrales
- PANICO   : VIX > 30 → no operar (aunque SPY > MA200)
"""

import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta


def _download_clean(ticker, start, end):
    """Descarga y aplana MultiIndex si existe."""
    raw = yf.download(ticker, start=start, end=end,
                      auto_adjust=True, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.reset_index()
    # Asegurar que Close es Series 1D
    close = raw['Close']
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    raw['Close'] = close.astype(float)
    return raw


def get_vix_level() -> dict:
    """Descarga el VIX actual y determina el nivel de volatilidad."""
    end   = datetime.today().strftime('%Y-%m-%d')
    start = (datetime.today() - timedelta(days=30)).strftime('%Y-%m-%d')
    try:
        df    = _download_clean('^VIX', start, end)
        vix   = float(df['Close'].iloc[-1])
        vix_5d= float(df['Close'].tail(5).mean())
        vix_20d=float(df['Close'].tail(20).mean())
        vix_trend = 'SUBIENDO' if vix > vix_20d * 1.1 else 'BAJANDO' if vix < vix_20d * 0.9 else 'ESTABLE'

        if vix < 20:
            level     = 'CALMO'
            level_icon= '🟢'
            size_adj  = 1.0
            vix_desc  = 'Volatilidad baja. Condiciones óptimas para mean reversion.'
            vix_color = '#22c55e'
            vix_score = 10
        elif vix < 25:
            level     = 'MODERADO'
            level_icon= '🟡'
            size_adj  = 0.75
            vix_desc  = 'Volatilidad moderada. Reduce tamaño de posición al 75%.'
            vix_color = '#fbbf24'
            vix_score = 7
        elif vix < 30:
            level     = 'ALTO'
            level_icon= '🟠'
            size_adj  = 0.50
            vix_desc  = 'Volatilidad alta. Tamaño al 50%. Sube umbrales (Score≥7, DD≥15%).'
            vix_color = '#f97316'
            vix_score = 3
        else:
            level     = 'PÁNICO'
            level_icon= '🔴'
            size_adj  = 0.0
            vix_desc  = 'Pánico de mercado. No operar aunque SPY esté por encima de MA200.'
            vix_color = '#ef4444'
            vix_score = 0

        return {
            'vix':         round(vix, 2),
            'vix_5d':      round(vix_5d, 2),
            'vix_20d':     round(vix_20d, 2),
            'vix_trend':   vix_trend,
            'vix_level':   level,
            'vix_icon':    level_icon,
            'vix_desc':    vix_desc,
            'vix_color':   vix_color,
            'vix_score':   vix_score,
            'size_adj':    size_adj,
        }
    except Exception as e:
        print(f"  ⚠ VIX error: {e}")
        return {
            'vix': 0, 'vix_5d': 0, 'vix_20d': 0, 'vix_trend': 'DESCONOCIDO',
            'vix_level': 'DESCONOCIDO', 'vix_icon': '⚪', 'vix_score': 5,
            'vix_desc': 'No se pudo obtener el VIX.', 'vix_color': '#6670a0',
            'size_adj': 1.0,
        }


def get_market_context() -> dict:
    """Descarga SPY + VIX y calcula el régimen combinado."""
    end   = datetime.today().strftime('%Y-%m-%d')
    start = (datetime.today() - timedelta(days=400)).strftime('%Y-%m-%d')

    try:
        spy = _download_clean('SPY', start, end)
        if spy.empty or len(spy) < 50:
            return _default_context()

        c = spy['Close']
        n = len(c)

        price   = float(c.iloc[-1])
        ma50    = float(c.rolling(50).mean().iloc[-1])
        ma200   = float(c.rolling(200).mean().iloc[-1]) if n >= 200 else float(c.mean())
        ma20    = float(c.rolling(20).mean().iloc[-1])

        # RSI
        delta = c.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = float((100 - 100 / (1 + gain / loss.replace(0, np.nan))).iloc[-1])

        # Drawdown 52 semanas
        high52   = float(c.iloc[-252:].max()) if n >= 252 else float(c.max())
        dd52     = (price / high52 - 1) * 100
        vs200    = (price / ma200 - 1) * 100

        # Pendiente MA200
        ma200_20d = float(c.rolling(200).mean().iloc[-21]) if n >= 221 else ma200
        ma200_slope = (ma200 - ma200_20d) / ma200_20d * 100

        # Régimen SPY
        if price > ma50 and ma50 > ma200 and vs200 > 3 and ma200_slope > 0:
            regime = 'ALCISTA FUERTE'; regime_color = '#22c55e'
            regime_icon = '🟢'; regime_score = 10
            regime_desc = 'Tendencia alcista sana. Condiciones favorables para mean reversion.'
        elif price > ma200 and vs200 > -2:
            regime = 'ALCISTA'; regime_color = '#86efac'
            regime_icon = '🟩'; regime_score = 8
            regime_desc = 'Por encima de MA200. Rebotes técnicos con alta probabilidad.'
        elif abs(vs200) <= 3:
            regime = 'LATERAL'; regime_color = '#fbbf24'
            regime_icon = '🟡'; regime_score = 5
            regime_desc = 'Mercado indeciso. Rebotes posibles pero menos predecibles.'
        elif vs200 > -15:
            regime = 'CORRECCIÓN'; regime_color = '#f97316'
            regime_icon = '🟠'; regime_score = 3
            regime_desc = 'Corrección activa. Reduce tamaño de posición al 50%.'
        else:
            regime = 'BAJISTA'; regime_color = '#ef4444'
            regime_icon = '🔴'; regime_score = 1
            regime_desc = 'Tendencia bajista. Rebotes son traps. No operar.'

    except Exception as e:
        print(f"  ⚠ SPY error: {e}")
        return _default_context()

    # Descargar VIX
    vix_data = get_vix_level()

    # ── RÉGIMEN COMBINADO SPY + VIX ──────────────────────────────
    combined_score = regime_score * 0.6 + vix_data['vix_score'] * 0.4

    # Si VIX en pánico, bloquear independientemente del SPY
    if vix_data['vix_level'] == 'PÁNICO':
        filter_rec   = 'NO OPERAR — VIX en pánico (>' + str(round(vix_data['vix'],0)) + ')'
        size_rec     = '0% — Esperar normalización del VIX'
        combined_rec = 'BLOQUEADO POR VIX'
    elif vix_data['vix_level'] == 'ALTO' or regime_score <= 3:
        filter_rec   = 'Muy selectivo (Score≥7.5, DD≥15%), tamaño 50%'
        size_rec     = '50% del tamaño normal'
        combined_rec = 'OPERAR CON CAUTELA'
    elif vix_data['vix_level'] == 'MODERADO' or regime_score == 5:
        filter_rec   = 'Selectivo (Score≥7.0, DD≥12%), tamaño 75%'
        size_rec     = '75% del tamaño normal'
        combined_rec = 'OPERAR CON PRECAUCIÓN'
    elif regime_score >= 8 and vix_data['vix_score'] >= 7:
        filter_rec   = 'Filtros validados (Score≥6.5, DD≥12%), tamaño 100%'
        size_rec     = '100% del tamaño normal'
        combined_rec = 'CONDICIONES ÓPTIMAS'
    else:
        filter_rec   = 'Filtros validados (Score≥6.5, DD≥12%), tamaño 75%'
        size_rec     = '75% del tamaño normal'
        combined_rec = 'OPERAR NORMAL'

    return {
        'date':              datetime.today().strftime('%Y-%m-%d %H:%M'),
        'spy_price':         round(price, 2),
        'spy_ma50':          round(ma50, 2),
        'spy_ma200':         round(ma200, 2),
        'spy_rsi':           round(rsi, 1),
        'spy_vs200':         round(vs200, 2),
        'spy_dd52':          round(dd52, 2),
        'spy_ma200_slope':   round(ma200_slope, 3),
        'market_regime':     regime,
        'regime_color':      regime_color,
        'regime_icon':       regime_icon,
        'regime_desc':       regime_desc,
        'regime_score':      regime_score,
        # VIX
        'vix':               vix_data['vix'],
        'vix_5d':            vix_data['vix_5d'],
        'vix_20d':           vix_data['vix_20d'],
        'vix_trend':         vix_data['vix_trend'],
        'vix_level':         vix_data['vix_level'],
        'vix_icon':          vix_data['vix_icon'],
        'vix_desc':          vix_data['vix_desc'],
        'vix_color':         vix_data['vix_color'],
        'vix_score':         vix_data['vix_score'],
        'size_adj':          vix_data['size_adj'],
        # Combinado
        'combined_score':    round(combined_score, 1),
        'filter_rec':        filter_rec,
        'size_rec':          size_rec,
        'combined_rec':      combined_rec,
    }


def _default_context() -> dict:
    return {
        'date': datetime.today().strftime('%Y-%m-%d %H:%M'),
        'spy_price': 0, 'spy_ma50': 0, 'spy_ma200': 0,
        'spy_rsi': 50, 'spy_vs200': 0, 'spy_dd52': 0, 'spy_ma200_slope': 0,
        'market_regime': 'DESCONOCIDO', 'regime_color': '#6670a0',
        'regime_icon': '⚪', 'regime_desc': 'No se pudo obtener el contexto de mercado.',
        'regime_score': 5,
        'vix': 0, 'vix_5d': 0, 'vix_20d': 0, 'vix_trend': 'DESCONOCIDO',
        'vix_level': 'DESCONOCIDO', 'vix_icon': '⚪', 'vix_desc': 'Sin datos VIX.',
        'vix_color': '#6670a0', 'vix_score': 5, 'size_adj': 1.0,
        'combined_score': 5, 'filter_rec': 'Filtros validados (Score≥6.5, DD≥12%)',
        'size_rec': '100% del tamaño normal', 'combined_rec': 'SIN DATOS',
    }


def calcular_mercado() -> dict:
    print("  ⏳ Descargando SPY + VIX...")
    ctx = get_market_context()
    print(f"  ✅ SPY: {ctx['regime_icon']} {ctx['market_regime']} | "
          f"VIX: {ctx['vix_icon']} {ctx['vix_level']} ({ctx['vix']:.1f})")
    print(f"     Recomendación: {ctx['combined_rec']} — {ctx['filter_rec']}")
    return ctx
