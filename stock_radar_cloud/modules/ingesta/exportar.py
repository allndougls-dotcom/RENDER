"""Exporta el Master CSV al disco local."""

import json
import pandas as pd
from pathlib import Path
from datetime import datetime

from ingesta.scoring import calcular_scores

DATA_DIR = Path(__file__).parent.parent.parent / "data" / "master"


def exportar_csv(sp500, df_tech, df_fund, df_earn, market_ctx, cfg) -> Path:

    df = calcular_scores(sp500, df_tech, df_fund)

    # Merge earnings
    if len(df_earn) > 0:
        df = df.merge(df_earn, on="ticker", how="left")
        df["earnings_days_next"] = df["earnings_days_next"].fillna(999).astype(int)
        df["earnings_warning"]   = df["earnings_warning"].fillna(False)
        print(f"  📅 Earnings: {(df['earnings_days_next']!=999).sum()} con fecha · ⚠ {df['earnings_warning'].sum()} en riesgo")
    else:
        df["earnings_days_next"] = 999
        df["earnings_date"]      = None
        df["earnings_warning"]   = False

    # Añadir contexto de mercado a cada fila
    df["market_regime"]       = market_ctx.get("market_regime", "DESCONOCIDO")
    df["market_regime_score"] = market_ctx.get("regime_score", 5)
    df["spy_price"]           = market_ctx.get("spy_price", 0)
    df["spy_vs200"]           = market_ctx.get("spy_vs200", 0)
    df["spy_rsi"]             = market_ctx.get("spy_rsi", 50)
    df["market_filter_rec"]   = market_ctx.get("filter_rec", "")

    # Guardar contexto de mercado también como JSON separado (para la app)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    market_json_path = DATA_DIR / "market_context.json"
    with open(market_json_path, 'w', encoding='utf-8') as f:
        json.dump(market_ctx, f, ensure_ascii=False, indent=2)

    date_tag = datetime.today().strftime("%Y%m%d")
    path     = DATA_DIR / f"sp500_full_export_{date_tag}.csv"

    export = df.drop(columns=["description"], errors="ignore")
    export.to_csv(path, index=False, float_format="%.4f")

    size_kb = path.stat().st_size / 1024
    print(f"\n  💾 Guardado en : {path}")
    print(f"     Tamaño      : {size_kb:.0f} KB · {len(export)} empresas · {len(export.columns)} columnas")
    print(f"  📊 Mercado     : {market_ctx['regime_icon']} {market_ctx['market_regime']} — {market_ctx['filter_rec']}")

    return path
