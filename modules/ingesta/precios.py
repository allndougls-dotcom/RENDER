"""Descarga series OHLCV desde yFinance.

OPTIMIZADO PARA MEMORIA (plan gratuito Render, límite 512MB):
- Lotes más pequeños (50 en vez de 100) reducen el pico de memoria
  temporal que genera yf.download() por cada iteración.
- Solo se conservan las columnas OHLCV que tecnico.py realmente usa
  (Open/High/Low/Close/Volume) — cualquier columna extra que yFinance
  pudiera añadir se descarta antes de guardar en el diccionario final,
  para no arrastrar peso muerto en memoria durante todo el pipeline.
- Cada DataFrame se convierte a tipos más compactos (float32 en vez
  de float64 para precios, que no necesitan esa precisión) — reduce
  aprox. a la mitad el tamaño en memoria de cada serie.
"""

import time
import gc
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from tqdm import tqdm

# Solo estas columnas se conservan — son las únicas que usa tecnico.py
# (Open en _candle_patterns, el resto en RSI/MACD/ATR/volumen/soportes)
COLUMNAS_NECESARIAS = ["Open", "High", "Low", "Close", "Volume"]


def descargar_precios(tickers: list, cfg: dict) -> dict:
    years   = cfg["YEARS_HISTORY"]
    end     = datetime.today().strftime("%Y-%m-%d")
    start   = (datetime.today() - timedelta(days=365 * years + 30)).strftime("%Y-%m-%d")

    print(f"  📅 Período: {start} → {end}")
    print(f"  🏢 Tickers: {len(tickers)}")

    BATCH   = 50  # reducido de 100 — baja el pico de memoria por lote
    prices  = {}
    batches = [tickers[i:i+BATCH] for i in range(0, len(tickers), BATCH)]

    for batch in tqdm(batches, desc="  Descargando lotes", unit="lote"):
        try:
            raw = yf.download(
                batch,
                start=start,
                end=end,
                auto_adjust=True,
                progress=False,
                group_by="ticker",
            )
            for t in batch:
                try:
                    df_t = raw[t].copy() if len(batch) > 1 else raw.copy()
                    df_t = df_t.dropna(subset=["Close"])
                    if len(df_t) > 50:
                        # Quedarnos solo con las columnas que se van a usar
                        # de verdad, descartando cualquier extra de yFinance
                        cols_presentes = [c for c in COLUMNAS_NECESARIAS if c in df_t.columns]
                        df_t = df_t[cols_presentes]
                        # float32 es suficiente precisión para precios/volumen
                        # y ocupa la mitad de memoria que el float64 por defecto
                        for c in ["Open", "High", "Low", "Close"]:
                            if c in df_t.columns:
                                df_t[c] = df_t[c].astype("float32")
                        if "Volume" in df_t.columns:
                            df_t["Volume"] = df_t["Volume"].astype("float32")
                        prices[t] = df_t
                except Exception:
                    pass
            # Liberar el DataFrame crudo del lote (puede tener MultiIndex
            # con TODOS los tickers del lote a la vez) antes del siguiente
            del raw
        except Exception as e:
            tqdm.write(f"  ⚠ Error en lote: {e}")
        gc.collect()
        time.sleep(0.8)

    ok  = len(prices)
    err = len(tickers) - ok
    print(f"  ✅ Precios OK: {ok}/{len(tickers)}" + (f" · Sin datos: {err}" if err else ""))
    return prices
