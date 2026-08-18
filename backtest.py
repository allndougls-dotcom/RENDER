"""
STOCK-RADAR · Backtesting Module v6
Walk-Forward Analysis + parametros optimizados

Uso:
  python backtest.py --rapido          -> comparativa variantes (50 tickers)
  python backtest.py --walkforward     -> walk-forward analysis
  python backtest.py --walkforward --rapido -> walk-forward rapido
"""

import argparse, json, time
import numpy as np
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import datetime, timedelta
from tqdm import tqdm

INITIAL_CAP   = 10_000.0
MAX_POSITIONS = 5
RISK_PCT      = 0.02

# Parametros optimizados segun backtest anterior
PARAMS_OPTIMOS = {
    'nombre':      'OPTIMO_WF',
    'rsi':         40,
    'dd':          12.0,
    'target_atr':  True,
    'atr_mult':    1.5,
    'stop_pct':    0.050,
    'max_hold':    15,
    'confirmacion':False,
    'time_stop':   10,
    'spy_filter':  True,
    'desc':        'ATR target + Time-stop 10d + SPY filter',
}

VARIANTES = [
    {'nombre':'BASE_OPTIMO','rsi':40,'dd':12.0,'target_atr':False,'target_pct':0.065,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'desc':'RSI<40 DD>=12% Target=6.5%'},
    {'nombre':'MEJORADO_ATR','rsi':40,'dd':12.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'desc':'Target dinamico 1.5xATR'},
    {'nombre':'TIME_STOP_10','rsi':40,'dd':12.0,'target_atr':False,'target_pct':0.065,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':10,'spy_filter':False,
     'desc':'Time-stop 10 dias'},
    {'nombre':'SPY_FILTER','rsi':40,'dd':12.0,'target_atr':False,'target_pct':0.065,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':True,
     'desc':'Filtro SPY>MA200'},
    {'nombre':'DD_8_ATR','rsi':40,'dd':8.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'desc':'Drawdown 8% (vs 12% base) + ATR dinamico'},
    {'nombre':'DD_10_ATR','rsi':40,'dd':10.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'desc':'Drawdown 10% (intermedio) + ATR dinamico'},
    {'nombre':'DD_4_ATR','rsi':40,'dd':4.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'desc':'Drawdown 4% (muy bajo) + ATR dinamico'},
    {'nombre':'DD_0_ATR','rsi':40,'dd':0.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'desc':'Sin filtro de drawdown (0%) + ATR dinamico'},
    {'nombre':'FULL_MEJORADO','rsi':40,'dd':12.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':10,'spy_filter':True,
     'desc':'ATR + Time-stop + SPY'},

    # ── GRUPO A — Baja volatilidad (ATR/Precio < 1.5%) ──────────
    # Variante A1: target conservador ajustado, stop estrecho, horizonte corto
    {'nombre':'GRUPO_A_v1','rsi':40,'dd':8.0,'target_atr':False,'target_pct':0.028,
     'stop_pct':0.022,'max_hold':8,'confirmacion':False,'time_stop':8,'spy_filter':False,
     'atr_filter':'LOW',   # solo empresas con ATR/Precio < 1.5%
     'desc':'Grupo A: target 2.8% stop 2.2% 8 dias (baja volatilidad)'},

    # Variante A2: stop amplio del 7% o cierre a las 2 semanas
    {'nombre':'GRUPO_A_v2','rsi':40,'dd':8.0,'target_atr':False,'target_pct':0.028,
     'stop_pct':0.07,'max_hold':14,'confirmacion':False,'time_stop':14,'spy_filter':False,
     'atr_filter':'LOW',   # solo empresas con ATR/Precio < 1.5%
     'desc':'Grupo A: target 2.8% stop 7% time-stop 14 dias'},

    # ── GRUPO B — Volatilidad media (ATR/Precio 1.5-3%) ─────────
    {'nombre':'GRUPO_B','rsi':40,'dd':12.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':12,'confirmacion':False,'time_stop':12,'spy_filter':False,
     'atr_filter':'MED',
     'desc':'Grupo B: ATR 1.5x stop 5% 12 dias (volatilidad media)'},

    # ── GRUPO C — Alta volatilidad (ATR/Precio > 3%) ─────────────
    {'nombre':'GRUPO_C','rsi':40,'dd':12.0,'target_atr':True,'atr_mult':1.8,
     'stop_pct':0.06,'max_hold':8,'confirmacion':False,'time_stop':8,'spy_filter':False,
     'atr_filter':'HIGH',
     'desc':'Grupo C: ATR 1.8x stop 6% 8 dias (alta volatilidad)'},

    # ── FILTRO DE CALIDAD FUNDAMENTAL (proxy score actual) ───────
    # Misma base que MEJORADO_ATR, pero exigiendo distintos umbrales
    # de fund_score >= X para aislar si el score aporta edge real
    # sobre el patron tecnico puro (RSI+Drawdown).
    {'nombre':'SCORE_65','rsi':40,'dd':12.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'min_fund_score':6.5,
     'desc':'Igual que MEJORADO_ATR + fund_score>=6.5 (calidad estandar app)'},
    {'nombre':'SCORE_75','rsi':40,'dd':12.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'min_fund_score':7.5,
     'desc':'Igual que MEJORADO_ATR + fund_score>=7.5 (alta calidad)'},
    {'nombre':'SCORE_50','rsi':40,'dd':12.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'min_fund_score':5.0,
     'desc':'Igual que MEJORADO_ATR + fund_score>=5.0 (filtro laxo)'},
    {'nombre':'SCORE_65_MAXWARN0','rsi':40,'dd':12.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'min_fund_score':6.5,'max_warnings':0,
     'desc':'fund_score>=6.5 + sin warnings activos (mas fiel a la app real)'},

    # ── COMBINADAS: mejor drawdown técnico + filtro de calidad ────
    # Buscan capturar el retorno alto de DD_10/DD_8 pero con el
    # MaxDD controlado que aporta el filtro de score fundamental.
    {'nombre':'DD10_SCORE65','rsi':40,'dd':10.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'min_fund_score':6.5,
     'desc':'Drawdown 10% + fund_score>=6.5 (combinacion optima hipotetica)'},
    {'nombre':'DD8_SCORE65','rsi':40,'dd':8.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'min_fund_score':6.5,
     'desc':'Drawdown 8% + fund_score>=6.5 (combinada)'},
    {'nombre':'DD10_SCORE75','rsi':40,'dd':10.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'min_fund_score':7.5,
     'desc':'Drawdown 10% + fund_score>=7.5 (alta calidad + mas señales)'},

    # ── VARIANTES BASADAS EN ANÁLISIS DE FALLOS (sobre DD10_SCORE65) ──
    # Analisis reveló: RSI 25-30 es zona muerta (WR 41%), Financials
    # tiene PnL medio NEGATIVO (-0.86%), y TIME_STOP tiene WR pobre (30%).
    {'nombre':'DD10_SCORE65_NORSI2530','rsi':40,'dd':10.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'min_fund_score':6.5,'rsi_exclude_range':(25.0, 30.0),
     'desc':'DD10_SCORE65 + excluye zona muerta RSI 25-30 (WR 41% en analisis fallos)'},
    {'nombre':'DD10_SCORE65_NOFINANCIALS','rsi':40,'dd':10.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'min_fund_score':6.5,'exclude_sectors':['Financials'],
     'desc':'DD10_SCORE65 + excluye sector Financials (PnL medio negativo en analisis fallos)'},
    {'nombre':'DD10_SCORE65_NOFINCOMM','rsi':40,'dd':10.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':15,'spy_filter':False,
     'min_fund_score':6.5,'exclude_sectors':['Financials','Communication Services'],
     'desc':'DD10_SCORE65 + excluye Financials y Communication Services (ambos peor WR en 4 analisis)'},
    {'nombre':'DD10_SCORE65_TS10','rsi':40,'dd':10.0,'target_atr':True,'atr_mult':1.5,
     'stop_pct':0.05,'max_hold':15,'confirmacion':False,'time_stop':10,'spy_filter':False,
     'min_fund_score':6.5,
     'desc':'DD10_SCORE65 + time_stop reducido a 10d (TIME_STOP tenia WR 30% en analisis fallos)'},
]

# Ventanas walk-forward
WF_WINDOWS = [
    # WF1: mercado alcista moderado (SPY +10% en el periodo)
    {'nombre':'WF1', 'train_start':'2024-04-01','train_end':'2024-10-01',
                     'test_start': '2024-10-01','test_end':  '2025-02-01'},
    # WF2: mercado alcista fuerte pre-corrección
    {'nombre':'WF2', 'train_start':'2024-08-01','train_end':'2025-02-01',
                     'test_start': '2025-02-01','test_end':  '2025-08-01'},
    # WF3: incluye corrección de aranceles feb-abril 2025
    {'nombre':'WF3', 'train_start':'2024-10-01','train_end':'2025-05-01',
                     'test_start': '2025-05-01','test_end':  '2025-11-01'},
    # WF4: periodo más reciente (2026), contexto actual
    {'nombre':'WF4', 'train_start':'2025-01-01','train_end':'2025-09-01',
                     'test_start': '2025-09-01','test_end':  '2026-05-20'},
]


def load_tickers():
    csvs = sorted((Path(__file__).parent/"data"/"master").glob("sp500_full_export_*.csv"))
    if csvs:
        t = pd.read_csv(csvs[-1])['ticker'].dropna().tolist()
        print(f"  Tickers CSV: {len(t)}")
        return t
    return ['AAPL','MSFT','GOOGL','AMZN','NVDA','META','JPM','JNJ','V','PG',
            'UNH','HD','MA','DIS','BAC','ADBE','CRM','NFLX','AMD','TMO',
            'ACN','AVGO','MCD','WMT','LIN','CAT','GS','RTX','HON','ISRG',
            'AMAT','LRCX','DE','AMGN','AXP','BKNG','COST','PEP','KO','XOM',
            'CVX','ABT','MRK','ABBV','LLY','NEE','PM','TXN','ORCL','SPGI']


def load_fundamental_scores():
    """
    Carga el score fundamental actual por ticker desde el CSV maestro.
    NOTA: esto es un PROXY histórico — usa el score de HOY aplicado
    retroactivamente a todo el periodo del backtest. Es una simplificación:
    asume que la calidad fundamental de una empresa grande y estable no
    cambia drásticamente en 2 años. Es menos válido para empresas en
    transformación fuerte (M&A, cambio de modelo de negocio, etc.)
    Devuelve dict: {ticker: {'fund_score':..., 'sector':..., 'warning_count':...}}
    """
    csvs = sorted((Path(__file__).parent/"data"/"master").glob("sp500_full_export_*.csv"))
    if not csvs:
        print("  ⚠ Sin CSV de fundamentales — el filtro de score no estará disponible")
        return {}

    df = pd.read_csv(csvs[-1])
    scores = {}
    for _, row in df.iterrows():
        ticker = row.get('ticker')
        if pd.isna(ticker):
            continue
        fund_score = row.get('fund_score', row.get('combined_score', np.nan))
        sector = row.get('sector', row.get('sector_gics', 'Unknown'))
        scores[ticker] = {
            'fund_score':    float(fund_score) if not pd.isna(fund_score) else 0.0,
            'sector':        str(sector) if not pd.isna(sector) else 'Unknown',
            'warning_count': int(row.get('warning_count', 0)) if not pd.isna(row.get('warning_count', 0)) else 0,
        }
    print(f"  Scores fundamentales cargados: {len(scores)} tickers (proxy actual aplicado al histórico)")
    return scores


def download_prices(tickers, years=2, start_date=None, end_date=None):
    """
    Carga precios históricos. Prioridad:
    1. CSVs locales descargados desde Colab (data/backtest_prices/)
    2. Descarga online desde yFinance (requiere internet, con pausas anti rate-limit)

    Si start_date/end_date se especifican (formato 'YYYY-MM-DD'), se usan
    directamente en vez del rango relativo de 'years' hacia atras desde hoy.
    Necesario para testear la estrategia en regimenes de mercado historicos
    (2022 bajista, 2020 COVID, 2015-16 lateral, etc.)
    """
    if start_date and end_date:
        start, end = start_date, end_date
    else:
        end   = datetime.today().strftime('%Y-%m-%d')
        start = (datetime.today()-timedelta(days=365*years+30)).strftime('%Y-%m-%d')
    print(f"  Periodo: {start} -> {end}")

    # Limpiar tickers
    tickers = [t.strip().lstrip('$').upper() for t in tickers]

    # ── Intentar cargar desde CSVs locales (descargados con Colab) ──
    local_dir = Path(__file__).parent / "data" / "backtest_prices"
    prices = {}

    if local_dir.exists():
        print(f"  Cargando desde CSVs locales ({local_dir.name})...")
        for t in tickers:
            csv_path = local_dir / f"{t}.csv"
            if csv_path.exists():
                try:
                    df = pd.read_csv(csv_path, parse_dates=['Date'])
                    df = df.dropna(subset=['Close'])
                    # Filtrar por periodo
                    df['Date'] = pd.to_datetime(df['Date'])
                    df = df[(df['Date'] >= start) & (df['Date'] <= end)]
                    if len(df) > 80:
                        prices[t] = df.reset_index(drop=True)
                except Exception:
                    pass
        if prices:
            print(f"  OK: {len(prices)}/{len(tickers)} cargados desde disco")
            missing = [t for t in tickers if t not in prices]
            if missing:
                print(f"  Sin CSV local: {len(missing)} tickers → intentando online...")
                tickers_online = missing
            else:
                return prices
        else:
            print(f"  No se encontraron CSVs locales, descargando online...")
            tickers_online = tickers
    else:
        tickers_online = tickers

    # ── Descarga online con pausas anti rate-limit ───────────────
    CHUNK_SIZE   = 15   # lotes más pequeños que antes (era 20)
    PAUSE_CHUNK  = 3.0  # segundos entre lotes
    MAX_RETRIES  = 3    # reintentos con backoff exponencial

    total_chunks = (len(tickers_online) + CHUNK_SIZE - 1) // CHUNK_SIZE
    failed = []

    for i in range(0, len(tickers_online), CHUNK_SIZE):
        chunk = tickers_online[i:i+CHUNK_SIZE]
        chunk_num = i // CHUNK_SIZE + 1
        print(f"  Lote {chunk_num}/{total_chunks} ({len(chunk)} tickers)...", end=' ')

        ok_in_chunk = 0
        try:
            raw = yf.download(chunk, start=start, end=end,
                              auto_adjust=True, progress=False,
                              group_by='ticker', threads=False)
            if raw.empty:
                failed.extend(chunk)
                print("vacío")
            else:
                if isinstance(raw.columns, pd.MultiIndex):
                    for t in chunk:
                        try:
                            df = raw[t].copy().dropna(subset=['Close'])
                            if len(df) > 80:
                                prices[t] = df.reset_index()
                                ok_in_chunk += 1
                            else:
                                failed.append(t)
                        except Exception:
                            failed.append(t)
                else:
                    t = chunk[0]
                    df = raw.copy().dropna(subset=['Close'])
                    if len(df) > 80:
                        prices[t] = df.reset_index()
                        ok_in_chunk += 1
                    else:
                        failed.append(t)
                print(f"OK {ok_in_chunk}/{len(chunk)}")
        except Exception as e:
            failed.extend(chunk)
            print(f"error ({str(e)[:40]})")

        # Pausa entre lotes para no saturar la API (excepto en el último)
        if i + CHUNK_SIZE < len(tickers_online):
            time.sleep(PAUSE_CHUNK)

    # ── Reintentar fallidos con backoff exponencial ──────────────
    if failed:
        print(f"  Reintentando {len(failed)} tickers fallidos (con pausas progresivas)...")
        still_failed = []
        for idx, t in enumerate(failed):
            success = False
            for attempt in range(MAX_RETRIES):
                try:
                    wait = 2 ** attempt  # 1, 2, 4 segundos
                    if attempt > 0:
                        time.sleep(wait)
                    df = yf.download(t, start=start, end=end,
                                     auto_adjust=True, progress=False)
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    df = df.dropna(subset=['Close'])
                    if len(df) > 80:
                        prices[t] = df.reset_index()
                        success = True
                        break
                except Exception:
                    continue
            if not success:
                still_failed.append(t)
            # Pausa breve cada pocos tickers para no reactivar el rate-limit
            if idx % 10 == 9:
                time.sleep(2.0)

        if still_failed:
            print(f"  Sin datos definitivamente: {len(still_failed)} tickers")
            print(f"  ({', '.join(still_failed[:15])}{'...' if len(still_failed) > 15 else ''})")

    print(f"  OK: {len(prices)}/{len(tickers)}")
    return prices


def download_spy(start_date=None, end_date=None):
    """Descarga SPY — version robusta compatible con todas las versiones de yFinance."""
    if start_date and end_date:
        # Necesitamos 200 dias extra ANTES del start_date para poder calcular la MA200
        # desde el primer dia real del periodo solicitado
        start = (pd.to_datetime(start_date) - timedelta(days=365)).strftime('%Y-%m-%d')
        end = end_date
    else:
        end   = datetime.today().strftime('%Y-%m-%d')
        start = (datetime.today()-timedelta(days=365*3)).strftime('%Y-%m-%d')
    try:
        raw = yf.download('SPY', start=start, end=end,
                          auto_adjust=True, progress=False)
        # Aplanar columnas MultiIndex si existen
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw = raw.reset_index()

        # Asegurar que Close es Series 1D
        close_col = raw['Close']
        if isinstance(close_col, pd.DataFrame):
            close_col = close_col.iloc[:, 0]
        c = close_col.astype(float)

        ma200 = c.rolling(200).mean()
        above = (c > ma200).values

        spy_dict = {}
        for i, row in raw.iterrows():
            date_str = str(row['Date'])[:10]
            if not pd.isna(above[i]):
                spy_dict[date_str] = bool(above[i])

        print(f"  SPY OK: {len(spy_dict)} dias · Actualmente {'ALCISTA' if list(spy_dict.values())[-1] else 'BAJISTA'}")
        return spy_dict
    except Exception as e:
        print(f"  SPY error: {e} — filtro desactivado")
        return {}


def build_indicators(prices):
    indicators = {}
    for ticker, df in prices.items():
        c = df['Close'].astype(float)
        h = df['High'].astype(float)
        lo = df['Low'].astype(float)
        v = df['Volume'].astype(float)

        delta = c.diff()
        g = delta.clip(lower=0).rolling(14).mean()
        l = (-delta.clip(upper=0)).rolling(14).mean()
        rsi = 100 - 100/(1 + g/l.replace(0, np.nan))

        ml   = c.ewm(span=12,adjust=False).mean() - c.ewm(span=26,adjust=False).mean()
        hist = ml - ml.ewm(span=9,adjust=False).mean()

        dd60 = (c - c.rolling(60,min_periods=20).max())/c.rolling(60,min_periods=20).max()*100
        vdec = v.rolling(5).mean() < v.rolling(60).mean()

        tr  = pd.concat([h-lo, (h-c.shift()).abs(), (lo-c.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()

        indicators[ticker] = {
            'rsi':       rsi.values,
            'hist':      hist.values,
            'dd60':      dd60.values,
            'vdec':      vdec.values,
            'atr':       atr.values,
            'close':     c.values,
            'high':      h.values,
            'prev_high': h.shift(1).values,
            'dates':     df['Date'].astype(str).str[:10].tolist(),
        }
    return indicators


def classify_atr_group(atr_val, price):
    """
    Clasifica empresa en grupo A/B/C segun ATR relativo al precio.
    Grupo A (LOW):  ATR/Precio < 1.5%  — defensivas, utilities, staples
    Grupo B (MED):  ATR/Precio 1.5-3%  — mayoria del S&P500
    Grupo C (HIGH): ATR/Precio > 3%    — semiconductores, biotech, growth
    """
    try:
        if price <= 0 or atr_val <= 0 or np.isnan(atr_val) or np.isnan(price):
            return "MED"
        atr_pct = (float(atr_val) / float(price)) * 100
        if atr_pct < 1.5:   return "LOW"
        elif atr_pct < 3.0: return "MED"
        else:                return "HIGH"
    except Exception:
        return "MED"


def get_median_atr_group(ticker_indicators):
    """
    Calcula el grupo ATR MEDIANO de un ticker a lo largo del periodo.
    Evita que una empresa cambie de grupo por un dia de volatilidad atipica.
    """
    ind = ticker_indicators
    n = len(ind["close"])
    groups = []
    for i in range(62, n):
        atr_i   = ind["atr"][i]
        price_i = ind["close"][i]
        if not np.isnan(atr_i) and not np.isnan(price_i) and price_i > 0:
            groups.append(classify_atr_group(atr_i, price_i))
    if not groups:
        return "MED"
    return max(set(groups), key=groups.count)


def build_signal_map(indicators, var, spy_dict, date_from=None, date_to=None, fund_scores=None):
    signal_map = {}
    n_sigs = 0
    atr_filter      = var.get("atr_filter", None)
    min_score       = var.get("min_fund_score", None)
    max_warnings    = var.get("max_warnings", None)
    exclude_sectors = var.get("exclude_sectors", None)  # lista de sectores a excluir
    rsi_exclude_range = var.get("rsi_exclude_range", None)  # tupla (min, max) a excluir

    # Pre-calcular grupo ATR mediano por ticker (mas estable que calcular por señal)
    ticker_atr_groups = {}
    if atr_filter is not None:
        for t, ind in indicators.items():
            ticker_atr_groups[t] = get_median_atr_group(ind)

    for ticker, ind in indicators.items():
        n = len(ind["rsi"])

        # Filtro ATR a nivel de ticker completo, no por señal individual
        if atr_filter is not None:
            if ticker_atr_groups.get(ticker, "MED") != atr_filter:
                continue

        # Filtro de calidad fundamental (proxy actual aplicado al histórico)
        if min_score is not None:
            if not fund_scores or ticker not in fund_scores:
                continue  # sin dato de score -> excluir (conservador)
            if fund_scores[ticker]['fund_score'] < min_score:
                continue

        # Filtro de warnings máximos permitidos
        if max_warnings is not None:
            if not fund_scores or ticker not in fund_scores:
                continue
            if fund_scores[ticker]['warning_count'] > max_warnings:
                continue

        # Filtro de exclusión de sector (ej. Financials, tras analisis de fallos)
        if exclude_sectors is not None:
            if not fund_scores or ticker not in fund_scores:
                continue
            if fund_scores[ticker].get('sector', 'Unknown') in exclude_sectors:
                continue

        ticker_sigs = {}


        for i in range(62, n - var['max_hold'] - 1):
            date_str = ind['dates'][i]

            if date_from and date_str < date_from: continue
            if date_to   and date_str >= date_to:  continue

            if var['spy_filter'] and spy_dict:
                if not spy_dict.get(date_str, True): continue

            r  = ind['rsi'][i]
            d  = ind['dd60'][i]
            h  = ind['hist'][i]
            hp = ind['hist'][i-1]
            vd = ind['vdec'][i]

            if not (not np.isnan(r) and r < var['rsi'] and
                    not np.isnan(d) and d <= -var['dd'] and
                    not np.isnan(h) and h > hp and bool(vd)):
                continue

            # Filtro de exclusión de rango RSI (ej. zona muerta 25-30 detectada en analisis de fallos)
            if rsi_exclude_range is not None:
                rmin, rmax = rsi_exclude_range
                if not np.isnan(r) and rmin <= r < rmax:
                    continue

            if var.get('confirmacion'):
                cl = ind['close'][i]
                ph = ind['prev_high'][i]
                if not (not np.isnan(ph) and cl > ph): continue

            entry = float(ind['close'][i])
            if var['target_atr'] and not np.isnan(ind['atr'][i]) and ind['atr'][i] > 0:
                tp = (entry + var['atr_mult'] * ind['atr'][i] - entry) / entry
            else:
                tp = var.get('target_pct', 0.065)

            atr_i   = ind['atr'][i] if not np.isnan(ind['atr'][i]) else 0
            atr_grp = classify_atr_group(atr_i, entry)
            ticker_sigs[date_str] = {
                'row_idx': i, 'entry': entry,
                'rsi': float(r), 'dd': float(d), 'target_pct': tp,
                'atr_val': round(atr_i, 2), 'atr_pct': round((atr_i/entry*100) if entry>0 else 0, 2),
                'atr_group': atr_grp,
            }

        if ticker_sigs:
            signal_map[ticker] = ticker_sigs
            n_sigs += len(ticker_sigs)

    return signal_map, n_sigs


def simulate_day_by_day(signal_map, prices, var, date_from=None, date_to=None, fund_scores=None):
    stop_pct  = var['stop_pct']
    time_stop = var['time_stop']

    # Filtrar fechas al periodo
    all_dates = sorted(set(
        str(row['Date'])[:10]
        for df in prices.values()
        for _, row in df.iterrows()
    ))
    if date_from: all_dates = [d for d in all_dates if d >= date_from]
    if date_to:   all_dates = [d for d in all_dates if d <  date_to]

    date_index = {}
    for ticker, df in prices.items():
        date_index[ticker] = {str(row['Date'])[:10]: row for _, row in df.iterrows()}

    capital  = INITIAL_CAP
    equity   = [capital]
    trades   = []
    open_pos = []

    for date in all_dates:
        still_open = []
        for pos in open_pos:
            row = date_index.get(pos['ticker'], {}).get(date)
            if row is None:
                pos['days_held'] = pos.get('days_held', 0) + 1
                still_open.append(pos)
                continue

            hi = float(row['High'])
            lo_p = float(row['Low'])
            cl = float(row['Close'])
            pos['days_held'] = pos.get('days_held', 0) + 1
            dh = pos['days_held']

            outcome = None
            exit_p  = cl

            if lo_p <= pos['stop']:
                outcome, exit_p = 'LOSS', pos['stop']
            elif hi >= pos['target']:
                outcome, exit_p = 'WIN',  pos['target']
            elif dh >= time_stop:
                outcome = 'WIN' if cl > pos['entry'] else 'LOSS'
                exit_p  = cl

            if outcome:
                pnl_pct = (exit_p - pos['entry']) / pos['entry']
                tp      = pos['target_pct']
                pnl_eur = pos['risk'] * (pnl_pct / stop_pct)
                pnl_eur = max(min(pnl_eur, pos['risk']*(tp/stop_pct)), -pos['risk'])
                capital = max(capital + pnl_eur, 1.0)
                equity.append(round(capital, 2))
                trades.append({
                    'ticker':pos['ticker'],'date':pos['date'],'exit_date':date,
                    'entry':round(pos['entry'],2),'exit':round(exit_p,2),
                    'outcome':outcome,'days':dh,
                    'pnl_pct':round(pnl_pct*100,2),'pnl_eur':round(pnl_eur,2),
                    'target_pct':round(tp*100,2),'rsi':pos['rsi'],'dd':pos['dd'],
                    'capital':round(capital,2),
                    'atr_group':pos.get('atr_group','MED'),
                    'atr_pct':pos.get('atr_pct',0),
                    'sector':pos.get('sector','Unknown'),
                    'fund_score':pos.get('fund_score',0),
                    'exit_reason': ('STOP' if lo_p <= pos['stop'] else 'TARGET' if hi >= pos['target'] else 'TIME_STOP'),
                })
            else:
                still_open.append(pos)

        open_pos = still_open

        open_tickers = {p['ticker'] for p in open_pos}
        slots = MAX_POSITIONS - len(open_pos)
        if slots > 0:
            for ticker, sig_dict in signal_map.items():
                if slots <= 0: break
                if ticker in open_tickers: continue
                if date not in sig_dict: continue
                sig = sig_dict[date]
                tp  = sig['target_pct']
                open_pos.append({
                    'ticker':ticker,'date':date,
                    'entry':sig['entry'],
                    'stop':sig['entry']*(1-stop_pct),
                    'target':sig['entry']*(1+tp),
                    'target_pct':tp,
                    'risk':capital*RISK_PCT,
                    'days_held':0,'rsi':sig['rsi'],'dd':sig['dd'],
                    'atr_group':sig.get('atr_group','MED'),
                    'atr_pct':sig.get('atr_pct',0),
                    'sector': (fund_scores.get(ticker, {}).get('sector', 'Unknown') if fund_scores else 'Unknown'),
                    'fund_score': (fund_scores.get(ticker, {}).get('fund_score', 0) if fund_scores else 0),
                })
                open_tickers.add(ticker)
                slots -= 1

    for pos in open_pos:
        df = prices.get(pos['ticker'])
        if df is not None and len(df) > 0:
            cl = float(df.iloc[-1]['Close'])
            pnl_pct = (cl-pos['entry'])/pos['entry']
            tp = pos['target_pct']
            pnl_eur = max(min(pos['risk']*(pnl_pct/stop_pct),
                              pos['risk']*(tp/stop_pct)),-pos['risk'])
            capital = max(capital+pnl_eur, 1.0)
            trades.append({'ticker':pos['ticker'],'date':pos['date'],'exit_date':'final',
                           'entry':round(pos['entry'],2),'exit':round(cl,2),
                           'outcome':'WIN' if pnl_pct>0 else 'LOSS',
                           'days':pos.get('days_held',time_stop),
                           'pnl_pct':round(pnl_pct*100,2),'pnl_eur':round(pnl_eur,2),
                           'target_pct':round(tp*100,2),'rsi':pos['rsi'],'dd':pos['dd'],
                           'capital':round(capital,2),
                           'sector':pos.get('sector','Unknown'),
                           'fund_score':pos.get('fund_score',0),
                           'exit_reason':'END_OF_PERIOD'})
        equity.append(round(capital,2))

    return trades, equity, capital


def calc_stats(trades, equity, final_cap, label=''):
    if not trades: return None
    df = pd.DataFrame(trades)
    total = len(df)
    wins  = (df['outcome']=='WIN').sum()
    losses = total-wins
    wr    = wins/total*100 if total>0 else 0
    ap    = df['pnl_pct'].mean()
    aw    = df[df['outcome']=='WIN']['pnl_pct'].mean() if wins>0 else 0
    al    = df[df['outcome']=='LOSS']['pnl_pct'].mean() if losses>0 else 0
    ad    = df['days'].mean()
    pf    = abs((aw*wins)/(al*losses)) if losses>0 and al!=0 else 999
    eq    = pd.Series(equity)
    mdd   = ((eq-eq.cummax())/eq.cummax()*100).min()

    by_t = df.groupby('ticker').agg(
        trades=('outcome','count'),wins=('outcome',lambda x:(x=='WIN').sum()),
        avg_pnl=('pnl_pct','mean'),total_eur=('pnl_eur','sum')
    ).reset_index()
    by_t['win_rate']=(by_t['wins']/by_t['trades']*100).round(1)
    by_t['avg_pnl']=by_t['avg_pnl'].round(2)
    by_t=by_t.sort_values('total_eur',ascending=False)

    df['month']=pd.to_datetime(df['date'],errors='coerce').dt.to_period('M').astype(str)
    by_m=df.groupby('month').agg(
        trades=('outcome','count'),
        win_rate=('outcome',lambda x:(x=='WIN').mean()*100),
        avg_pnl=('pnl_pct','mean'),pnl_eur=('pnl_eur','sum')
    ).reset_index()
    by_m['win_rate']=by_m['win_rate'].round(1)
    by_m['avg_pnl']=by_m['avg_pnl'].round(2)

    return {
        'label': label,
        'summary':{
            'total_trades':int(total),'wins':int(wins),'losses':int(losses),
            'win_rate':round(float(wr),1),'avg_pnl':round(float(ap),2),
            'avg_win':round(float(aw),2),'avg_loss':round(float(al),2),
            'avg_days':round(float(ad),1),'profit_factor':round(float(pf),2),
            'final_capital':round(float(final_cap),2),
            'total_return':round((final_cap/INITIAL_CAP-1)*100,1),
            'max_drawdown':round(float(mdd),1),
        },
        'by_ticker': by_t.head(20).to_dict('records'),
        'by_month':  by_m.to_dict('records'),
        'equity_curve': equity[::max(1,len(equity)//300)],
        'trades': df.to_dict('records'),  # TODOS los trades, no solo los últimos 200
    }


def analyze_failures(trades, label=''):
    """
    Analiza qué tienen en COMUN las operaciones perdedoras (LOSS) vs ganadoras (WIN).
    Compara distribuciones de RSI, DD, sector, dia de semana, fund_score, ATR group,
    motivo de salida y meses, para detectar patrones explotables en los fallos.
    """
    if not trades:
        print("  Sin trades para analizar")
        return None

    df = pd.DataFrame(trades)
    df['date_parsed'] = pd.to_datetime(df['date'], errors='coerce')
    df['weekday'] = df['date_parsed'].dt.day_name()
    df['month']   = df['date_parsed'].dt.to_period('M').astype(str)

    wins   = df[df['outcome'] == 'WIN']
    losses = df[df['outcome'] == 'LOSS']

    print(f"""
  ══════════════════════════════════════════════════════════════════════════════════
  ANÁLISIS DE FALLOS — {label}
  ══════════════════════════════════════════════════════════════════════════════════
  Total operaciones : {len(df)}
  Ganadoras (WIN)    : {len(wins)} ({len(wins)/len(df)*100:.1f}%)
  Perdedoras (LOSS)  : {len(losses)} ({len(losses)/len(df)*100:.1f}%)
  ══════════════════════════════════════════════════════════════════════════════════""")

    if len(losses) == 0:
        print("  Sin operaciones perdedoras — nada que analizar")
        return None

    # ── 1. Motivo de salida ───────────────────────────────────────
    print(f"\n  [1] MOTIVO DE SALIDA (WIN vs LOSS)")
    print(f"  {'Motivo':<14} {'Wins':>8} {'Losses':>8} {'% del total LOSS':>18}")
    print(f"  {'-'*54}")
    if 'exit_reason' in df.columns:
        for reason in df['exit_reason'].dropna().unique():
            w = (wins['exit_reason'] == reason).sum() if 'exit_reason' in wins.columns else 0
            l = (losses['exit_reason'] == reason).sum() if 'exit_reason' in losses.columns else 0
            pct_of_losses = l / len(losses) * 100 if len(losses) > 0 else 0
            print(f"  {reason:<14} {w:>8} {l:>8} {pct_of_losses:>17.1f}%")

    # ── 2. RSI de entrada ──────────────────────────────────────────
    print(f"\n  [2] RSI DE ENTRADA")
    print(f"    WIN  → RSI medio: {wins['rsi'].mean():.1f} (mediana: {wins['rsi'].median():.1f})")
    print(f"    LOSS → RSI medio: {losses['rsi'].mean():.1f} (mediana: {losses['rsi'].median():.1f})")
    # Buckets de RSI
    bins = [0, 20, 25, 30, 35, 40]
    labels_rsi = ['<20', '20-25', '25-30', '30-35', '35-40']
    df['rsi_bucket'] = pd.cut(df['rsi'], bins=bins, labels=labels_rsi)
    print(f"\n    {'RSI rango':<10} {'Ops':>6} {'WR%':>7}")
    for b in labels_rsi:
        sub = df[df['rsi_bucket'] == b]
        if len(sub) > 0:
            wr_b = (sub['outcome'] == 'WIN').mean() * 100
            print(f"    {b:<10} {len(sub):>6} {wr_b:>6.1f}%")

    # ── 3. Drawdown de entrada ──────────────────────────────────────
    print(f"\n  [3] DRAWDOWN DE ENTRADA (60d)")
    print(f"    WIN  → DD medio: -{wins['dd'].mean():.1f}%")
    print(f"    LOSS → DD medio: -{losses['dd'].mean():.1f}%")
    bins_dd = [0, 10, 15, 20, 30, 100]
    labels_dd = ['0-10%', '10-15%', '15-20%', '20-30%', '30%+']
    df['dd_bucket'] = pd.cut(df['dd'], bins=bins_dd, labels=labels_dd)
    print(f"\n    {'DD rango':<10} {'Ops':>6} {'WR%':>7}")
    for b in labels_dd:
        sub = df[df['dd_bucket'] == b]
        if len(sub) > 0:
            wr_b = (sub['outcome'] == 'WIN').mean() * 100
            print(f"    {b:<10} {len(sub):>6} {wr_b:>6.1f}%")

    # ── 4. Sector ───────────────────────────────────────────────────
    if 'sector' in df.columns and df['sector'].nunique() > 1:
        print(f"\n  [4] POR SECTOR (ordenado por peor WR)")
        by_sector = df.groupby('sector').agg(
            ops=('outcome', 'count'),
            wr=('outcome', lambda x: (x == 'WIN').mean() * 100),
            avg_pnl=('pnl_pct', 'mean'),
        ).reset_index().sort_values('wr')
        print(f"    {'Sector':<28} {'Ops':>6} {'WR%':>7} {'PnL medio':>10}")
        for _, row in by_sector.iterrows():
            if row['ops'] >= 10:  # solo sectores con muestra mínima
                flag = ' ⚠' if row['wr'] < 45 else ''
                print(f"    {row['sector']:<28} {row['ops']:>6.0f} {row['wr']:>6.1f}% {row['avg_pnl']:>+9.2f}%{flag}")

    # ── 5. Score fundamental ─────────────────────────────────────────
    if 'fund_score' in df.columns:
        print(f"\n  [5] SCORE FUNDAMENTAL DE ENTRADA")
        print(f"    WIN  → Score medio: {wins['fund_score'].mean():.2f}")
        print(f"    LOSS → Score medio: {losses['fund_score'].mean():.2f}")
        bins_sc = [0, 6.5, 7.0, 7.5, 8.0, 10]
        labels_sc = ['6.5-7.0', '7.0-7.5', '7.5-8.0', '8.0-9.0', '9.0+']
        df['score_bucket'] = pd.cut(df['fund_score'], bins=bins_sc, labels=labels_sc)
        print(f"\n    {'Score rango':<12} {'Ops':>6} {'WR%':>7}")
        for b in labels_sc:
            sub = df[df['score_bucket'] == b]
            if len(sub) > 0:
                wr_b = (sub['outcome'] == 'WIN').mean() * 100
                print(f"    {b:<12} {len(sub):>6} {wr_b:>6.1f}%")

    # ── 6. Día de la semana ───────────────────────────────────────
    print(f"\n  [6] DÍA DE LA SEMANA DE ENTRADA")
    day_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
    print(f"    {'Día':<10} {'Ops':>6} {'WR%':>7} {'PnL medio':>10}")
    for day in day_order:
        sub = df[df['weekday'] == day]
        if len(sub) > 0:
            wr_d = (sub['outcome'] == 'WIN').mean() * 100
            pnl_d = sub['pnl_pct'].mean()
            flag = ' ⚠' if wr_d < 45 else ''
            print(f"    {day:<10} {len(sub):>6} {wr_d:>6.1f}% {pnl_d:>+9.2f}%{flag}")

    # ── 7. Días en posición ──────────────────────────────────────
    print(f"\n  [7] DÍAS EN POSICIÓN")
    print(f"    WIN  → días medios: {wins['days'].mean():.1f}")
    print(f"    LOSS → días medios: {losses['days'].mean():.1f}")

    # ── 8. Grupo ATR (volatilidad) ────────────────────────────────
    if 'atr_group' in df.columns and df['atr_group'].nunique() > 1:
        print(f"\n  [8] POR GRUPO DE VOLATILIDAD (ATR)")
        by_atr = df.groupby('atr_group').agg(
            ops=('outcome', 'count'),
            wr=('outcome', lambda x: (x == 'WIN').mean() * 100),
        ).reset_index()
        for _, row in by_atr.iterrows():
            print(f"    {row['atr_group']:<6} {row['ops']:>6.0f} ops  WR={row['wr']:.1f}%")

    # ── 9. Meses con peor rendimiento ────────────────────────────
    print(f"\n  [9] MESES CON PEOR WIN RATE (top 5)")
    by_month_fail = df.groupby('month').agg(
        ops=('outcome', 'count'),
        wr=('outcome', lambda x: (x == 'WIN').mean() * 100),
    ).reset_index().sort_values('wr')
    print(f"    {'Mes':<10} {'Ops':>6} {'WR%':>7}")
    for _, row in by_month_fail.head(5).iterrows():
        if row['ops'] >= 5:
            print(f"    {row['month']:<10} {row['ops']:>6.0f} {row['wr']:>6.1f}%")

    # ── 10. Top tickers perdedores ────────────────────────────────
    print(f"\n  [10] TICKERS CON MÁS PÉRDIDA ACUMULADA")
    by_ticker_loss = df.groupby('ticker').agg(
        ops=('outcome', 'count'),
        wr=('outcome', lambda x: (x == 'WIN').mean() * 100),
        total_eur=('pnl_eur', 'sum'),
    ).reset_index().sort_values('total_eur').head(10)
    print(f"    {'Ticker':<8} {'Ops':>5} {'WR%':>7} {'PnL total':>12}")
    for _, row in by_ticker_loss.iterrows():
        if row['total_eur'] < 0:
            print(f"    {row['ticker']:<8} {row['ops']:>5.0f} {row['wr']:>6.1f}% {row['total_eur']:>+11.2f}€")

    print(f"\n  ══════════════════════════════════════════════════════════════════════════════════")

    return {
        'label': label,
        'total_trades': len(df),
        'wins': len(wins),
        'losses': len(losses),
        'by_sector': by_sector.to_dict('records') if 'sector' in df.columns and df['sector'].nunique() > 1 else [],
        'by_rsi_bucket': df.groupby('rsi_bucket', observed=True).agg(
            ops=('outcome','count'), wr=('outcome', lambda x: (x=='WIN').mean()*100)
        ).reset_index().to_dict('records'),
        'by_dd_bucket': df.groupby('dd_bucket', observed=True).agg(
            ops=('outcome','count'), wr=('outcome', lambda x: (x=='WIN').mean()*100)
        ).reset_index().to_dict('records'),
        'by_weekday': df.groupby('weekday').agg(
            ops=('outcome','count'), wr=('outcome', lambda x: (x=='WIN').mean()*100)
        ).reset_index().to_dict('records'),
        'worst_tickers': by_ticker_loss.to_dict('records'),
    }


def run_walkforward(tickers, prices, indicators, spy_dict, fund_scores=None):
    """
    Walk-Forward Analysis:
    Para cada ventana temporal:
      1. Periodo TRAIN: encontrar mejor variante
      2. Periodo TEST: aplicar esa variante out-of-sample
    """
    print(f"""
  ══════════════════════════════════════════════
  WALK-FORWARD ANALYSIS
  Ventanas: {len(WF_WINDOWS)} · Parametros: {len(VARIANTES)} variantes por ventana
  ══════════════════════════════════════════════""")

    wf_results = []

    for wf in WF_WINDOWS:
        print(f"\n  [{wf['nombre']}]")
        print(f"    TRAIN: {wf['train_start']} -> {wf['train_end']}")
        print(f"    TEST : {wf['test_start']}  -> {wf['test_end']}")

        # ── TRAIN: encontrar mejor variante ─────────────────────
        print(f"\n    Buscando mejor variante en periodo TRAIN...")
        train_results = []
        for var in VARIANTES:
            smap, n = build_signal_map(indicators, var, spy_dict,
                                       wf['train_start'], wf['train_end'],
                                       fund_scores=fund_scores)
            if not smap: continue
            t, eq, cap = simulate_day_by_day(
                smap, prices, var, wf['train_start'], wf['train_end'], fund_scores=fund_scores)
            s = calc_stats(t, eq, cap, var['nombre'])
            if s:
                train_results.append((var, s))

        if not train_results:
            print(f"    Sin señales en periodo TRAIN — saltando")
            continue

        # Filtrar variantes con menos de 15 trades en TRAIN (no estadisticamente significativas)
        train_results = [(v, s) for v, s in train_results
                         if s["summary"]["total_trades"] >= 15]

        if not train_results:
            print(f"    Insuficientes operaciones en TRAIN — saltando")
            continue

        # Elegir por PROFIT FACTOR (mas robusto que retorno bruto)
        # El retorno bruto favorece variantes con muchas ops (como GRUPO_A_v1 con ATR roto)
        # El PF mide calidad por operacion, independiente del volumen
        best_var, best_train = max(
            train_results,
            key=lambda x: (x[1]["summary"]["profit_factor"], x[1]["summary"]["win_rate"])
        )
        st = best_train["summary"]
        print(f"    Mejor en TRAIN: {best_var['nombre']} "
              f"(PF={st['profit_factor']:.2f}x "
              f"WR={st['win_rate']:.1f}% "
              f"Ret={st['total_return']:+.1f}% "
              f"Ops={st['total_trades']})")

        # ── TEST: aplicar out-of-sample ──────────────────────────
        print(f"\n    Aplicando {best_var['nombre']} en periodo TEST (out-of-sample)...")
        smap_test, n_test = build_signal_map(indicators, best_var, spy_dict,
                                              wf['test_start'], wf['test_end'],
                                              fund_scores=fund_scores)
        if not smap_test:
            print(f"    Sin señales en periodo TEST")
            test_stats = None
        else:
            t_test, eq_test, cap_test = simulate_day_by_day(
                smap_test, prices, best_var, wf['test_start'], wf['test_end'], fund_scores=fund_scores)
            test_stats = calc_stats(t_test, eq_test, cap_test, f"{best_var['nombre']}_TEST")

        if test_stats:
            st = test_stats['summary']
            print(f"    TEST resultado: Ret={st['total_return']:+.1f}% "
                  f"WR={st['win_rate']:.1f}% PF={st['profit_factor']:.2f}x "
                  f"MDD={st['max_drawdown']:.1f}%")

        wf_results.append({
            'ventana':       wf['nombre'],
            'train_period':  f"{wf['train_start']} -> {wf['train_end']}",
            'test_period':   f"{wf['test_start']} -> {wf['test_end']}",
            'best_variante': best_var['nombre'],
            'best_desc':     best_var['desc'],
            'train_stats':   best_train['summary'],
            'test_stats':    test_stats['summary'] if test_stats else None,
        })

    # ── RESUMEN WALK-FORWARD ────────────────────────────────────
    print(f"""
  ══════════════════════════════════════════════════════════════════════════════════
  RESUMEN WALK-FORWARD ({len(WF_WINDOWS)} ventanas · {len(VARIANTES)} variantes por ventana)
  ══════════════════════════════════════════════════════════════════════════════════
  {'Ventana':<6} {'Mejor variante':<18} {'TRAIN':>8} {'TEST':>8} {'WR':>7} {'PF':>6} {'MDD':>8} {''}
  {'-'*80}""")

    test_returns = []
    wf_by_variante = {}  # acumular resultados TEST por variante

    for r in wf_results:
        tr = r['train_stats']['total_return']
        ts = r['test_stats']
        if ts:
            te_ret = ts['total_return']
            te_wr  = ts['win_rate']
            te_mdd = ts['max_drawdown']
            te_pf  = ts['profit_factor']
            test_returns.append(te_ret)
            consistent = '✓' if te_ret > 0 else '✗'

            # Acumular por variante
            var = r['best_variante']
            if var not in wf_by_variante:
                wf_by_variante[var] = []
            wf_by_variante[var].append({
                'ret': te_ret, 'wr': te_wr, 'mdd': te_mdd, 'pf': te_pf
            })
        else:
            te_ret = te_wr = te_mdd = te_pf = 0
            consistent = '—'

        print(f"  {r['ventana']:<6} {r['best_variante']:<18} "
              f"{tr:>+7.1f}% {te_ret:>+7.1f}% {te_wr:>6.1f}% "
              f"{te_pf:>5.2f}x {te_mdd:>7.1f}% {consistent}")

    if test_returns:
        avg_test = sum(test_returns)/len(test_returns)
        positive = sum(1 for r in test_returns if r > 0)

        print(f"\n  {'─'*80}")
        print(f"  Retorno TEST promedio : {avg_test:+.1f}%")
        print(f"  Ventanas positivas    : {positive}/{len(test_returns)}")

        if avg_test > 5 and positive >= int(len(test_returns)*0.66):
            veredicto = "✅ EDGE CONFIRMADO — El sistema funciona out-of-sample"
        elif avg_test > 0 and positive >= len(test_returns)//2:
            veredicto = "⚠  EDGE DEBIL — Positivo pero marginal, operar con cautela"
        else:
            veredicto = "❌ SIN EDGE — Los parametros no generalizan bien"

        print(f"  Veredicto             : {veredicto}")

        # ── Tabla resumen por variante ──────────────────────────
        if wf_by_variante:
            print(f"\n  RENDIMIENTO TEST POR VARIANTE (cuando fue elegida como mejor en TRAIN):")
            print(f"  {'Variante':<18} {'Veces elegida':>14} {'Ret medio':>10} {'WR medio':>9} {'PF medio':>9}")
            print(f"  {'─'*65}")
            for var, resultados in sorted(wf_by_variante.items(),
                                          key=lambda x: sum(r['ret'] for r in x[1])/len(x[1]),
                                          reverse=True):
                avg_r  = sum(r['ret'] for r in resultados)/len(resultados)
                avg_wr = sum(r['wr']  for r in resultados)/len(resultados)
                avg_pf = sum(r['pf']  for r in resultados)/len(resultados)
                veces  = len(resultados)
                mark   = ' ← MEJOR' if avg_r == max(sum(r['ret'] for r in v)/len(v) for v in wf_by_variante.values()) else ''
                print(f"  {var:<18} {veces:>14} {avg_r:>+9.1f}% {avg_wr:>8.1f}% {avg_pf:>8.2f}x{mark}")

        # ── Análisis GRUPO_A_v2 específico ──────────────────────
        if 'GRUPO_A_v2' in wf_by_variante:
            r_a2 = wf_by_variante['GRUPO_A_v2']
            avg_r = sum(r['ret'] for r in r_a2)/len(r_a2)
            avg_wr= sum(r['wr']  for r in r_a2)/len(r_a2)
            pos   = sum(1 for r in r_a2 if r['ret'] > 0)
            print(f"\n  GRUPO_A_v2 (72.1% WR en backtest completo):")
            print(f"    Elegida como mejor : {len(r_a2)} ventana(s)")
            print(f"    Retorno TEST medio : {avg_r:+.1f}%")
            print(f"    WR TEST medio      : {avg_wr:.1f}%")
            print(f"    Ventanas positivas : {pos}/{len(r_a2)}")
            if avg_wr >= 60:
                print(f"    → ✅ Win rate alto se confirma out-of-sample")
            elif avg_wr >= 50:
                print(f"    → ⚠  Win rate se reduce pero sigue siendo positivo")
            else:
                print(f"    → ❌ Win rate del 72% era overfitting — no usar")
        else:
            print(f"\n  GRUPO_A_v2: no fue seleccionada como mejor en ninguna ventana TRAIN")
            print(f"    → Significa que otras variantes superaron su retorno en TRAIN")
            print(f"    → Esto NO invalida su uso — puede funcionar bien como capa complementaria")

    return wf_results


def run_variantes(tickers, prices, indicators, spy_dict, rapido=False, fund_scores=None, failures=False):
    """Comparativa de variantes (modo original)."""
    all_results = []
    trades_by_variant = {}
    for i, var in enumerate(VARIANTES):
        print(f"\n  [{i+1}/{len(VARIANTES)}] {var['nombre']}: {var['desc']}")
        smap, n = build_signal_map(indicators, var, spy_dict, fund_scores=fund_scores)
        print(f"        Señales: {n}")
        if not smap:
            print("        Sin señales — saltando")
            continue
        t, eq, cap = simulate_day_by_day(smap, prices, var, fund_scores=fund_scores)
        s = calc_stats(t, eq, cap, var['nombre'])
        if s:
            s['variante']   = var['nombre']
            s['descripcion']= var['desc']
            s['params']     = var
            all_results.append(s)
            trades_by_variant[var['nombre']] = t
            st = s['summary']
            print(f"        WR={st['win_rate']:.1f}% PF={st['profit_factor']:.2f}x "
                  f"Ret={st['total_return']:+.1f}% MDD={st['max_drawdown']:.1f}%")

    if not all_results: return

    best = max(all_results, key=lambda r: r['summary']['total_return'])
    print(f"\n  << Mejor: {best['variante']} — {best['descripcion']}")

    out = Path(__file__).parent/"data"/"master"
    out.mkdir(parents=True, exist_ok=True)
    with open(out/"backtest_results.json",'w',encoding='utf-8') as f:
        json.dump(best, f, ensure_ascii=False, indent=2, default=str)
    with open(out/"backtest_all_variants.json",'w',encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"  Guardado: backtest_results.json")

    # ── Análisis de fallos (opcional, --failures) ────────────────
    if failures:
        for var_name, trades_list in trades_by_variant.items():
            fail_result = analyze_failures(trades_list, label=var_name)
            if fail_result:
                fail_path = out / f"failure_analysis_{var_name}.json"
                with open(fail_path, 'w', encoding='utf-8') as f:
                    json.dump(fail_result, f, ensure_ascii=False, indent=2, default=str)
                print(f"  Guardado: failure_analysis_{var_name}.json")

    return all_results


def main(rapido=False, walkforward=False, ticker=None, solo=None, failures=False, historical=False):
    global VARIANTES
    if solo:
        original_count = len(VARIANTES)
        VARIANTES = [v for v in VARIANTES if v['nombre'] in solo]
        nombres_encontrados = [v['nombre'] for v in VARIANTES]
        nombres_no_encontrados = [n for n in solo if n not in nombres_encontrados]
        if nombres_no_encontrados:
            print(f"  ⚠ No encontradas: {nombres_no_encontrados}")
        print(f"  Modo --solo: ejecutando {len(VARIANTES)}/{original_count} variantes → {nombres_encontrados}")

    tickers = [ticker.upper()] if ticker else load_tickers()
    if rapido:
        tickers = tickers[:50]
        print(f"\n  Modo rapido: {len(tickers)} tickers")

    # Cargar scores fundamentales (proxy actual aplicado al histórico)
    fund_scores = load_fundamental_scores()
    if fund_scores:
        con_score = sum(1 for t in tickers if t in fund_scores and fund_scores[t]['fund_score'] >= 6.5)
        print(f"  Tickers con fund_score >= 6.5: {con_score}/{len(tickers)}")

    if historical:
        run_historical_regimes(tickers, fund_scores=fund_scores)
        return

    prices = download_prices(tickers)
    if not prices: return

    print(f"\n  Pre-calculando indicadores...")
    indicators = build_indicators(prices)

    # Mostrar distribucion de grupos ATR
    groups_dist = {t: get_median_atr_group(ind) for t, ind in indicators.items()}
    cnt = {g: sum(1 for v in groups_dist.values() if v == g) for g in ["LOW","MED","HIGH"]}
    print(f"  Grupos ATR: A(LOW/baja)={cnt['LOW']} B(MED/media)={cnt['MED']} C(HIGH/alta)={cnt['HIGH']}")

    print(f"  Descargando SPY...")
    spy_dict = download_spy()

    out = Path(__file__).parent/"data"/"master"
    out.mkdir(parents=True, exist_ok=True)

    if walkforward:
        wf_results = run_walkforward(tickers, prices, indicators, spy_dict, fund_scores=fund_scores)
        with open(out/"walkforward_results.json",'w',encoding='utf-8') as f:
            json.dump(wf_results, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n  Guardado: walkforward_results.json")
    else:
        run_variantes(tickers, prices, indicators, spy_dict, rapido, fund_scores=fund_scores, failures=failures)




# ══════════════════════════════════════════════════════════════════
# VALIDACIÓN EN REGÍMENES DE MERCADO HISTÓRICOS
# Prueba una variante en periodos con caracteristicas de mercado
# distintas al periodo reciente (2024-2026), para detectar si el
# edge es estructural o especifico de este ciclo de mercado.
# Uso: python backtest.py --historical --solo DD10_SCORE65_NOFINCOMM
# ══════════════════════════════════════════════════════════════════

HISTORICAL_REGIMES = [
    {
        'nombre': '2022_BAJISTA',
        'start': '2022-01-01', 'end': '2022-12-31',
        'desc': 'Mercado bajista — subida agresiva de tipos Fed, SPY -19% en el año',
    },
    {
        'nombre': '2020_COVID',
        'start': '2020-01-01', 'end': '2020-12-31',
        'desc': 'Shock + recuperacion en V — crash 34% en 5 semanas, rally posterior',
    },
    {
        'nombre': '2015_2016_LATERAL',
        'start': '2015-06-01', 'end': '2016-06-30',
        'desc': 'Mercado lateral/sin tendencia clara — miedo a China, petroleo bajo',
    },
    {
        'nombre': '2017_ALCISTA_DISTINTO',
        'start': '2017-01-01', 'end': '2017-12-31',
        'desc': 'Alcista con volatilidad historicamente muy baja (VIX medio ~11)',
    },
]


def run_historical_regimes(tickers, fund_scores=None):
    """
    Ejecuta las VARIANTES actualmente filtradas (via --solo) en cada uno
    de los regimenes historicos definidos en HISTORICAL_REGIMES.

    IMPORTANTE — limitaciones a tener en cuenta al interpretar resultados:
    1. El universo de 503 tickers es el ACTUAL del S&P500. En 2015-2017 o
       2020-2022 la composicion del indice era distinta (supervivencia
       sesgada: empresas que quebraron/fueron excluidas no aparecen aqui).
    2. El fund_score usado sigue siendo el PROXY actual — no hay forma de
       saber el score fundamental real que tenia cada empresa en 2016.
       Este test valida sobre todo la componente TECNICA (RSI+DD+ATR) del
       sistema en distintos regimenes, no tanto la combinacion completa.
    3. Los resultados de este test son por tanto una validacion parcial,
       no una prueba definitiva. Sirven para detectar si el patron tecnico
       base es robusto entre regimenes o si depende del ciclo 2024-2026.
    """
    print(f"""
  ══════════════════════════════════════════════════════════════════════════════════
  VALIDACIÓN EN REGÍMENES DE MERCADO HISTÓRICOS
  Variantes: {[v['nombre'] for v in VARIANTES]}
  ══════════════════════════════════════════════════════════════════════════════════
  ⚠ LIMITACIONES: universo de tickers es el actual (sesgo de supervivencia).
  ⚠ fund_score es un PROXY actual, no el score real de esa epoca.
  Este test valida sobre todo el componente TECNICO del sistema.
  ══════════════════════════════════════════════════════════════════════════════════""")

    all_regime_results = []

    for regime in HISTORICAL_REGIMES:
        print(f"\n  ── [{regime['nombre']}] {regime['start']} -> {regime['end']}")
        print(f"     {regime['desc']}")

        prices = download_prices(tickers, start_date=regime['start'], end_date=regime['end'])
        if not prices:
            print(f"     ⚠ Sin datos de precios para este periodo — saltando")
            continue

        indicators = build_indicators(prices)
        spy_dict = download_spy(start_date=regime['start'], end_date=regime['end'])

        for var in VARIANTES:
            smap, n = build_signal_map(indicators, var, spy_dict,
                                       date_from=regime['start'], date_to=regime['end'],
                                       fund_scores=fund_scores)
            if not smap:
                print(f"     [{var['nombre']}] Sin señales en este régimen")
                continue

            t, eq, cap = simulate_day_by_day(smap, prices, var,
                                             date_from=regime['start'], date_to=regime['end'],
                                             fund_scores=fund_scores)
            s = calc_stats(t, eq, cap, var['nombre'])
            if s:
                st = s['summary']
                print(f"     [{var['nombre']}] Señales={n} WR={st['win_rate']:.1f}% "
                      f"PF={st['profit_factor']:.2f}x Ret={st['total_return']:+.1f}% "
                      f"MDD={st['max_drawdown']:.1f}%")
                all_regime_results.append({
                    'regime': regime['nombre'],
                    'regime_desc': regime['desc'],
                    'variante': var['nombre'],
                    'señales': n,
                    'summary': st,
                })
            else:
                print(f"     [{var['nombre']}] Señales={n} pero sin trades cerrados")

    # ── Resumen final ──────────────────────────────────────────────
    print(f"""
  ══════════════════════════════════════════════════════════════════════════════════
  RESUMEN — RENDIMIENTO POR RÉGIMEN HISTÓRICO
  ══════════════════════════════════════════════════════════════════════════════════
  {'Régimen':<22} {'Variante':<26} {'Ops':>5} {'WR':>7} {'PF':>7} {'Retorno':>9} {'MDD':>8}
  {'-'*90}""")

    positive_count = 0
    total_count = 0
    for r in all_regime_results:
        st = r['summary']
        ok = '✓' if st['total_return'] > 0 else '✗'
        if st['total_return'] > 0:
            positive_count += 1
        total_count += 1
        print(f"  {r['regime']:<22} {r['variante']:<26} {st['total_trades']:>5} "
              f"{st['win_rate']:>6.1f}% {st['profit_factor']:>6.2f}x "
              f"{st['total_return']:>+8.1f}% {st['max_drawdown']:>7.1f}% {ok}")

    print(f"  {'-'*90}")
    if total_count > 0:
        print(f"  Regímenes positivos: {positive_count}/{total_count}")
        if positive_count == total_count:
            veredicto = "✅ EDGE ROBUSTO — Positivo en TODOS los regímenes históricos probados"
        elif positive_count >= total_count * 0.75:
            veredicto = "✅ EDGE MAYORMENTE ROBUSTO — Positivo en la mayoría de regímenes"
        elif positive_count >= total_count * 0.5:
            veredicto = "⚠  EDGE DEPENDIENTE DE RÉGIMEN — Funciona en unos contextos y en otros no"
        else:
            veredicto = "❌ EDGE FRÁGIL — Probablemente específico del ciclo 2024-2026, no estructural"
        print(f"  Veredicto: {veredicto}")
    else:
        print("  Sin resultados suficientes para veredicto")

    out = Path(__file__).parent/"data"/"master"
    out.mkdir(parents=True, exist_ok=True)
    with open(out/"historical_regimes_results.json", 'w', encoding='utf-8') as f:
        json.dump(all_regime_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  Guardado: historical_regimes_results.json")

    return all_regime_results



def download_spy_vix(years=2):
    """Descarga SPY y VIX histórico y construye un mapa de régimen por fecha."""
    import yfinance as yf
    from datetime import datetime, timedelta
    end   = datetime.today().strftime('%Y-%m-%d')
    start = (datetime.today()-timedelta(days=365*years+30)).strftime('%Y-%m-%d')

    regime_map = {}  # date -> {'spy_regime', 'vix', 'vix_level', 'regime_full'}

    try:
        # SPY
        raw = yf.download('SPY', start=start, end=end, auto_adjust=True, progress=False)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        spy = raw.reset_index()
        c   = spy['Close'].astype(float)
        ma200 = c.rolling(200).mean()
        ma50  = c.rolling(50).mean()
        spy_regime_series = []
        for i in range(len(spy)):
            p, m200, m50 = float(c.iloc[i]), float(ma200.iloc[i]) if not np.isnan(ma200.iloc[i]) else 0, float(ma50.iloc[i]) if not np.isnan(ma50.iloc[i]) else 0
            vs200 = (p/m200-1)*100 if m200 > 0 else 0
            if p > m50 and m50 > m200 and vs200 > 3:   r = 'ALCISTA_FUERTE'
            elif p > m200 and vs200 > -2:               r = 'ALCISTA'
            elif abs(vs200) <= 3:                        r = 'LATERAL'
            elif vs200 > -15:                            r = 'CORRECCION'
            else:                                        r = 'BAJISTA'
            spy_regime_series.append(r)
        spy['spy_regime'] = spy_regime_series
        print(f"  SPY OK: {len(spy)} dias")
    except Exception as e:
        print(f"  SPY error: {e}")
        return {}

    try:
        # VIX
        raw2 = yf.download('^VIX', start=start, end=end, auto_adjust=True, progress=False)
        if isinstance(raw2.columns, pd.MultiIndex):
            raw2.columns = raw2.columns.get_level_values(0)
        vix = raw2.reset_index()
        vix['vix'] = vix['Close'].astype(float)
        vix_dict = {str(row['Date'])[:10]: float(row['vix']) for _, row in vix.iterrows()}
        print(f"  VIX OK: {len(vix_dict)} dias")
    except Exception as e:
        print(f"  VIX error: {e} - usando VIX=18 por defecto")
        vix_dict = {}

    # Construir mapa completo
    for _, row in spy.iterrows():
        date_str  = str(row['Date'])[:10]
        spy_reg   = row['spy_regime']
        vix_val   = vix_dict.get(date_str, 18.0)

        if vix_val < 18:        vix_level = 'VIX_BAJO'
        elif vix_val < 25:      vix_level = 'VIX_MODERADO'
        elif vix_val < 30:      vix_level = 'VIX_ALTO'
        else:                   vix_level = 'VIX_PANICO'

        # Régimen combinado
        if spy_reg in ('ALCISTA_FUERTE','ALCISTA') and vix_val < 18:
            regime_full = 'OPTIMO'          # Condiciones ideales
        elif spy_reg in ('ALCISTA_FUERTE','ALCISTA') and vix_val < 25:
            regime_full = 'FAVORABLE'       # Condiciones buenas
        elif spy_reg in ('ALCISTA_FUERTE','ALCISTA') and vix_val < 30:
            regime_full = 'CAUTELOSO'       # Operar con cuidado
        elif spy_reg == 'LATERAL':
            regime_full = 'LATERAL'         # Zona gris
        elif vix_val >= 30:
            regime_full = 'PANICO'          # No operar
        else:
            regime_full = 'ADVERSO'         # Correccion/Bajista

        regime_map[date_str] = {
            'spy_regime':   spy_reg,
            'vix':          round(vix_val, 1),
            'vix_level':    vix_level,
            'regime_full':  regime_full,
        }

    return regime_map


def run_regime_analysis():
    """
    Carga el backtest_all_variants.json existente y añade análisis por régimen.
    No descarga nuevos datos de precios — solo SPY y VIX para clasificar regímenes.
    """
    import json
    from pathlib import Path

    out_dir = Path(__file__).parent / "data" / "master"
    variants_file = out_dir / "backtest_all_variants.json"
    results_file  = out_dir / "backtest_results.json"

    # Buscar trades en cualquier archivo disponible
    all_trades = []
    source_file = None

    if variants_file.exists():
        with open(variants_file, 'r', encoding='utf-8') as f:
            variants = json.load(f)
        # Usar la variante con más trades
        best = max(variants, key=lambda v: v['summary']['total_trades'])
        all_trades = best.get('trades', [])
        source_file = variants_file
        print(f"  Usando variante: {best['variante']} ({len(all_trades)} trades)")
    elif results_file.exists():
        with open(results_file, 'r', encoding='utf-8') as f:
            results = json.load(f)
        all_trades = results.get('trades', [])
        source_file = results_file

    if not all_trades:
        print("  No hay trades disponibles. Ejecuta primero: python backtest.py --rapido")
        return

    print(f"\n  Descargando SPY + VIX para clasificar regímenes...")
    regime_map = download_spy_vix()

    if not regime_map:
        print("  No se pudo obtener datos de régimen")
        return

    # Enriquecer trades con régimen
    df = pd.DataFrame(all_trades)
    df['regime_full'] = df['date'].apply(
        lambda d: regime_map.get(str(d)[:10], {}).get('regime_full', 'DESCONOCIDO')
    )
    df['spy_regime'] = df['date'].apply(
        lambda d: regime_map.get(str(d)[:10], {}).get('spy_regime', '—')
    )
    df['vix_entry'] = df['date'].apply(
        lambda d: regime_map.get(str(d)[:10], {}).get('vix', 0)
    )

    # ── ESTADÍSTICAS POR RÉGIMEN ────────────────────────────────
    print(f"""
╔══════════════════════════════════════════════════════════════════════════════════╗
║  ANÁLISIS POR RÉGIMEN SPY + VIX                                                  ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║  {'Régimen':<20} {'Ops':>5} {'WinRate':>8} {'PnL med':>8} {'PF':>6} {'Ret€':>8} {'VIX med':>8} ║
╠══════════════════════════════════════════════════════════════════════════════════╣""")

    orden = ['OPTIMO', 'FAVORABLE', 'CAUTELOSO', 'LATERAL', 'ADVERSO', 'PANICO', 'DESCONOCIDO']
    regime_stats = []

    for reg in orden:
        sub = df[df['regime_full'] == reg]
        if len(sub) == 0:
            continue
        total  = len(sub)
        wins   = (sub['outcome'] == 'WIN').sum()
        losses = total - wins
        wr     = wins/total*100
        ap     = sub['pnl_pct'].mean()
        aw     = sub[sub['outcome']=='WIN']['pnl_pct'].mean() if wins > 0 else 0
        al     = sub[sub['outcome']=='LOSS']['pnl_pct'].mean() if losses > 0 else 0
        pf     = abs((aw*wins)/(al*losses)) if losses > 0 and al != 0 else 999
        ret_eur= sub['pnl_eur'].sum() if 'pnl_eur' in sub.columns else 0
        vix_m  = sub['vix_entry'].mean()

        print(f"║  {reg:<20} {total:>5} {wr:>7.1f}% {ap:>+7.2f}% {pf:>6.2f}x {ret_eur:>+8.0f}€ {vix_m:>7.1f}  ║")
        regime_stats.append({
            'regime': reg, 'trades': total, 'win_rate': round(wr,1),
            'avg_pnl': round(ap,2), 'profit_factor': round(pf,2),
            'total_eur': round(float(ret_eur),2), 'vix_mean': round(vix_m,1),
            'avg_win': round(aw,2), 'avg_loss': round(al,2),
        })

    print(f"╚══════════════════════════════════════════════════════════════════════════════════╝")

    # ── ANÁLISIS DE TARGETS POR VOLATILIDAD ────────────────────
    print(f"""
╔══════════════════════════════════════════════════════════════════════════════════╗
║  ANÁLISIS POR NIVEL DE VIX                                                       ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║  {'VIX Range':<18} {'Ops':>5} {'WinRate':>8} {'PnL med':>8} {'PF':>6} {'Días med':>9}  ║
╠══════════════════════════════════════════════════════════════════════════════════╣""")

    vix_ranges = [
        ('VIX < 15 (muy bajo)',    (0, 15)),
        ('VIX 15-18 (bajo)',       (15, 18)),
        ('VIX 18-22 (normal)',     (18, 22)),
        ('VIX 22-27 (elevado)',    (22, 27)),
        ('VIX > 27 (alto/pánico)', (27, 999)),
    ]

    for label, (lo, hi) in vix_ranges:
        sub = df[(df['vix_entry'] >= lo) & (df['vix_entry'] < hi)]
        if len(sub) == 0:
            continue
        total = len(sub)
        wins  = (sub['outcome'] == 'WIN').sum()
        losses= total - wins
        wr    = wins/total*100
        ap    = sub['pnl_pct'].mean()
        aw    = sub[sub['outcome']=='WIN']['pnl_pct'].mean() if wins > 0 else 0
        al    = sub[sub['outcome']=='LOSS']['pnl_pct'].mean() if losses > 0 else 0
        pf    = abs((aw*wins)/(al*losses)) if losses > 0 and al != 0 else 999
        ad    = sub['days'].mean() if 'days' in sub.columns else 0
        print(f"║  {label:<18} {total:>5} {wr:>7.1f}% {ap:>+7.2f}% {pf:>6.2f}x {ad:>8.1f}d  ║")

    print(f"╚══════════════════════════════════════════════════════════════════════════════════╝")

    # ── RECOMENDACIONES ─────────────────────────────────────────
    print(f"""
╔══════════════════════════════════════════════════════════════════════════════════╗
║  RECOMENDACIONES BASADAS EN DATOS                                                 ║
╠══════════════════════════════════════════════════════════════════════════════════╣""")

    best_regime = max(regime_stats, key=lambda x: x['profit_factor']) if regime_stats else None
    worst_regime= min(regime_stats, key=lambda x: x['win_rate']) if regime_stats else None

    if best_regime:
        print(f"║  Mejor régimen  : {best_regime['regime']:<20} PF={best_regime['profit_factor']:.2f}x WR={best_regime['win_rate']:.1f}%{'':>17}║")
    if worst_regime:
        print(f"║  Peor régimen   : {worst_regime['regime']:<20} PF={worst_regime['profit_factor']:.2f}x WR={worst_regime['win_rate']:.1f}%{'':>17}║")

    # Régimen actual
    today = pd.Timestamp.today().strftime('%Y-%m-%d')
    today_regime = regime_map.get(today, {})
    if not today_regime:
        # Buscar el dia mas reciente disponible
        recent = sorted(regime_map.keys())[-1]
        today_regime = regime_map[recent]
        today = recent

    reg_hoy = today_regime.get('regime_full', '—')
    vix_hoy = today_regime.get('vix', 0)
    spy_hoy = today_regime.get('spy_regime', '—')

    print(f"║  {'─'*76}║")
    print(f"║  Régimen HOY ({today}): {spy_hoy} + VIX {vix_hoy:.1f} → {reg_hoy:<20}{'':>5}║")

    # Buscar estadísticas del régimen actual
    hoy_stats = next((r for r in regime_stats if r['regime'] == reg_hoy), None)
    if hoy_stats:
        print(f"║  En este régimen históricamente: WR={hoy_stats['win_rate']:.1f}% PF={hoy_stats['profit_factor']:.2f}x PnL med={hoy_stats['avg_pnl']:+.2f}%{'':>14}║")
        if hoy_stats['profit_factor'] >= 1.3:
            rec = "Operar con parametros estandar"
        elif hoy_stats['profit_factor'] >= 1.0:
            rec = "Operar con cautela, reducir tamano 50%"
        else:
            rec = "Evitar operar en este regimen"
        print(f"║  Recomendacion: {rec:<60}║")

    print(f"╚══════════════════════════════════════════════════════════════════════════════════╝")

    # Guardar resultado
    regime_output = {
        'generated_at':  pd.Timestamp.today().isoformat(),
        'regime_stats':  regime_stats,
        'current_regime': today_regime,
        'current_date':   today,
    }
    with open(out_dir / "regime_analysis.json", 'w', encoding='utf-8') as f:
        json.dump(regime_output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  Guardado: regime_analysis.json")
    return regime_output


# ── CLI ───────────────────────────────────────────────────────────
if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--rapido',      action='store_true')
    p.add_argument('--walkforward', action='store_true')
    p.add_argument('--ticker',      type=str)
    p.add_argument('--regimen',     action='store_true',
                    help='Analisis de regimen de mercado (SPY+VIX) sobre resultados existentes')
    p.add_argument('--historical',  action='store_true',
                    help='Prueba la(s) variante(s) en 4 regimenes de mercado historicos: '
                         '2022 bajista, 2020 COVID, 2015-16 lateral, 2017 alcista baja volatilidad. '
                         'Usar junto con --solo para elegir la variante a validar.')
    p.add_argument('--solo',        type=str,
                    help='Nombre(s) de variante(s) a ejecutar, separadas por coma. '
                         'Ej: --solo DD10_SCORE65  o  --solo DD10_SCORE65,SCORE_65')
    p.add_argument('--failures',    action='store_true',
                    help='Genera analisis de fallos (que comparten las operaciones LOSS) '
                         'por sector, RSI, drawdown, score, dia semana, etc.')
    args = p.parse_args()

    if args.regimen:
        run_regime_analysis()
    else:
        solo_list = [s.strip() for s in args.solo.split(',')] if args.solo else None
        main(rapido=args.rapido, walkforward=args.walkforward, ticker=args.ticker,
             solo=solo_list, failures=args.failures, historical=args.historical)

