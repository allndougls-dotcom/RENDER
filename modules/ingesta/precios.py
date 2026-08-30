"""Descarga series OHLCV desde yFinance."""

import time
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from tqdm import tqdm


def descargar_precios(tickers: list, cfg: dict) -> dict:
    years   = cfg["YEARS_HISTORY"]
    end     = datetime.today().strftime("%Y-%m-%d")
    start   = (datetime.today() - timedelta(days=365 * years + 30)).strftime("%Y-%m-%d")

    print(f"  📅 Período: {start} → {end}")
    print(f"  🏢 Tickers: {len(tickers)}")

    BATCH   = 100
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
                        prices[t] = df_t
                except Exception:
                    pass
        except Exception as e:
            tqdm.write(f"  ⚠ Error en lote: {e}")
        time.sleep(0.8)

    ok  = len(prices)
    err = len(tickers) - ok
    print(f"  ✅ Precios OK: {ok}/{len(tickers)}" + (f" · Sin datos: {err}" if err else ""))
    return prices
