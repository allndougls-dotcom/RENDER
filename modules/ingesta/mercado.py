"""Contexto de mercado para SIDI.

Desde SIDI_INTRADAY_V2 el régimen descriptivo NO modifica dinámicamente los
umbrales. La puerta de mercado operativa congelada es SPY20 <= +1% y el VIX
queda como información, no como filtro.
"""
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta

HISTORY_DAYS = 250
SIDI_STRATEGY_VERSION = "SIDI_INTRADAY_V2"
SIDI_MAX_SPY20 = 1.0


def get_market_context() -> dict:
    try:
        end = datetime.today().strftime('%Y-%m-%d')
        start = (datetime.today() - timedelta(days=365 * 2 + 60)).strftime('%Y-%m-%d')
        spy = yf.download('SPY', start=start, end=end, auto_adjust=True, progress=False)
        if spy.empty or len(spy) < 50:
            return _default_context()
        closes = spy['Close'].squeeze()
        n = len(closes)
        price = float(closes.iloc[-1])
        ma20 = float(closes.rolling(20).mean().iloc[-1])
        ma50 = float(closes.rolling(50).mean().iloc[-1])
        ma200 = float(closes.rolling(200).mean().iloc[-1]) if n >= 200 else float(closes.rolling(n).mean().iloc[-1])
        spy20 = (float(closes.iloc[-1]) / float(closes.iloc[-21]) - 1.0) * 100.0 if n >= 21 and float(closes.iloc[-21]) > 0 else np.nan

        delta = closes.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi_series = (100 - 100 / (1 + rs)).round(2)
        rsi = float(rsi_series.iloc[-1])
        high52 = float(closes.iloc[-252:].max()) if n >= 252 else float(closes.max())
        dd52 = (price / high52 - 1) * 100
        vs200 = (price / ma200 - 1) * 100
        ma200_20d_ago = float(closes.rolling(200).mean().iloc[-21]) if n >= 221 else ma200
        ma200_slope = (ma200 - ma200_20d_ago) / ma200_20d_ago * 100
        vol20 = float(closes.pct_change().rolling(20).std().iloc[-1]) * 100
        vol60 = float(closes.pct_change().rolling(60).std().iloc[-1]) * 100
        vol_ratio = vol20 / vol60 if vol60 > 0 else 1.0

        ma200_series = closes.rolling(200).mean()
        history = []
        for d, c, m, r in zip(spy.index[-HISTORY_DAYS:], closes.iloc[-HISTORY_DAYS:], ma200_series.iloc[-HISTORY_DAYS:], rsi_series.iloc[-HISTORY_DAYS:]):
            history.append({'date': d.strftime('%Y-%m-%d'), 'close': round(float(c), 2),
                            'ma200': round(float(m), 2) if not pd.isna(m) else None,
                            'rsi': round(float(r), 1) if not pd.isna(r) else None})

        vix_value = None
        try:
            vix_data = yf.download('^VIX', start=start, end=end, auto_adjust=True, progress=False)
            if not vix_data.empty:
                vix_value = round(float(vix_data['Close'].squeeze().iloc[-1]), 2)
        except Exception as e:
            print(f"  ⚠ Error obteniendo VIX (no crítico): {e}")

        if price > ma50 and ma50 > ma200 and vs200 > 3 and ma200_slope > 0:
            regime, regime_color, regime_icon, regime_score = 'ALCISTA FUERTE', '#22c55e', '🟢', 10
        elif price > ma200 and vs200 > -2:
            regime, regime_color, regime_icon, regime_score = 'ALCISTA', '#86efac', '🟩', 8
        elif abs(vs200) <= 3:
            regime, regime_color, regime_icon, regime_score = 'LATERAL', '#fbbf24', '🟡', 5
        elif vs200 > -15:
            regime, regime_color, regime_icon, regime_score = 'CORRECCIÓN', '#f97316', '🟠', 3
        else:
            regime, regime_color, regime_icon, regime_score = 'BAJISTA', '#ef4444', '🔴', 1
        regime_desc = f'{regime}. Régimen descriptivo; SIDI_INTRADAY_V2 usa SPY20 como puerta operativa.'
        spy_gate = bool(np.isfinite(spy20) and spy20 <= SIDI_MAX_SPY20)
        filter_rec = ('SIDI_INTRADAY_V2: Fund≥6.5 · DD60≥12% · RSI<40 · '
                      'MACD↑ · Vol↓ · SPY20≤+1% · Abnormal20≤-10%')
        return {
            'date': datetime.today().strftime('%Y-%m-%d %H:%M'),
            'sidi_strategy_version': SIDI_STRATEGY_VERSION,
            'spy_price': round(price, 2), 'spy_ma20': round(ma20, 2),
            'spy_ma50': round(ma50, 2), 'spy_ma200': round(ma200, 2),
            'spy_rsi': round(rsi, 1),
            'spy_return_20d': round(float(spy20), 4) if np.isfinite(spy20) else None,
            'sidi_spy20_gate': spy_gate,
            'spy_vs200': round(vs200, 2), 'spy_dd52': round(dd52, 2),
            'spy_ma200_slope': round(ma200_slope, 3), 'spy_vol_ratio': round(vol_ratio, 2),
            'vix': vix_value, 'market_regime': regime, 'regime_color': regime_color,
            'regime_icon': regime_icon, 'regime_desc': regime_desc,
            'regime_score': regime_score, 'filter_rec': filter_rec, 'history': history,
        }
    except Exception as e:
        print(f"  ⚠ Error obteniendo contexto de mercado: {e}")
        return _default_context()


def _default_context() -> dict:
    return {
        'date': datetime.today().strftime('%Y-%m-%d %H:%M'),
        'sidi_strategy_version': SIDI_STRATEGY_VERSION,
        'spy_price': 0, 'spy_ma20': 0, 'spy_ma50': 0, 'spy_ma200': 0, 'spy_rsi': 50,
        'spy_return_20d': None, 'sidi_spy20_gate': False, 'spy_vs200': 0,
        'spy_dd52': 0, 'spy_ma200_slope': 0, 'spy_vol_ratio': 1, 'vix': None,
        'market_regime': 'DESCONOCIDO', 'regime_color': '#6670a0', 'regime_icon': '⚪',
        'regime_desc': 'No se pudo obtener el contexto de mercado.', 'regime_score': 5,
        'filter_rec': 'SIDI_INTRADAY_V2: contexto SPY20 no disponible', 'history': [],
    }


def calcular_mercado() -> dict:
    print("  ⏳ Descargando contexto de mercado (SPY + VIX)...")
    ctx = get_market_context()
    print(f"  ✅ Mercado: {ctx['regime_icon']} {ctx['market_regime']}")
    vix_str = f"{ctx['vix']}" if ctx['vix'] is not None else "N/D"
    spy20 = ctx.get('spy_return_20d')
    spy20_str = f"{spy20:+.2f}%" if spy20 is not None else "N/D"
    print(f"     SPY: ${ctx['spy_price']} | SPY20: {spy20_str} | vs MA200: {ctx['spy_vs200']:+.1f}% | VIX: {vix_str}")
    print(f"     Estrategia: {ctx['filter_rec']}")
    return ctx
