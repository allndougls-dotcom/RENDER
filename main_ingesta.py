"""
╔══════════════════════════════════════════════════════════════════╗
║   STOCK-RADAR · FASE A · main_ingesta.py                        ║
║   Uso:                                                           ║
║     python main_ingesta.py              → proceso completo      ║
║     python main_ingesta.py --test       → solo 20 empresas      ║
║     python main_ingesta.py --solo-tech  → solo técnico          ║
║     python main_ingesta.py --solo-fund  → solo fundamental      ║
║     python main_ingesta.py --solo-earn  → solo earnings dates   ║
║                                                                    ║
║   OPTIMIZADO PARA MEMORIA (plan gratuito Render, límite 512MB): ║
║   - El contexto SIDI_INTRADAY_V2 (SPY20 + Abnormal20) se calcula  ║
║     antes de liberar all_prices porque necesita las series       ║
║     históricas ya descargadas.                                  ║
║   - gc.collect() forzado entre pasos para recuperar memoria.    ║
╚══════════════════════════════════════════════════════════════════╝
"""

import argparse, sys, gc
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent / "modules"))

from ingesta.tickers      import get_sp500_tickers
from ingesta.precios      import descargar_precios
from ingesta.tecnico      import calcular_tecnicos
from ingesta.sidi_context import calcular_contexto_sidi
from ingesta.fundamental  import calcular_fundamentales
from ingesta.earnings     import calcular_earnings
from ingesta.mercado      import calcular_mercado
from ingesta.exportar     import exportar_csv
from config_loader        import cargar_config
from shadow_tracker       import update_shadow_tracker


def _log_memoria(etiqueta: str):
    try:
        import psutil, os
        mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
        print(f"  🧠 Memoria tras {etiqueta}: {mb:.0f} MB", flush=True)
    except ImportError:
        pass


def main():
    parser = argparse.ArgumentParser(description="Stock-Radar · Ingesta S&P500")
    parser.add_argument("--test",       action="store_true")
    parser.add_argument("--solo-tech",  action="store_true")
    parser.add_argument("--solo-fund",  action="store_true")
    parser.add_argument("--solo-earn",  action="store_true")
    args = parser.parse_args()

    cfg = cargar_config()
    if args.test:
        cfg["MAX_EMPRESAS"] = 20
        print("🧪 Modo TEST — 20 empresas")

    print(f"""
╔══════════════════════════════════════════════╗
║  STOCK-RADAR · Fase A · Ingesta              ║
╠══════════════════════════════════════════════╣
║  Empresas : {cfg['MAX_EMPRESAS']:<32}║
║  Historial: {cfg['YEARS_HISTORY']} años{'':<27}║
║  FMP API  : {'✅ Configurada' if cfg['USE_FMP'] else '⚠️  No configurada (yFinance)':<32}║
║  Inicio   : {datetime.now().strftime('%H:%M:%S'):<32}║
╚══════════════════════════════════════════════╝
""")

    import pandas as pd

    print("━" * 50)
    print("PASO 1/7 · Componentes S&P500")
    sp500 = get_sp500_tickers(cfg["MAX_EMPRESAS"])
    _log_memoria("PASO 1")

    print("\n" + "━" * 50)
    print("PASO 2/7 · Contexto de mercado (SPY)")
    market_ctx = calcular_mercado()
    _log_memoria("PASO 2")

    if not args.solo_fund and not args.solo_earn:
        print("\n" + "━" * 50)
        print("PASO 3/7 · Precios OHLCV (yFinance)")
        all_prices = descargar_precios(sp500["ticker"].tolist(), cfg)
    else:
        all_prices = {}
    _log_memoria("PASO 3 (precios en memoria)")

    if not args.solo_fund and not args.solo_earn:
        print("\n" + "━" * 50)
        print("PASO 4/7 · Indicadores técnicos")
        df_tech = calcular_tecnicos(all_prices)

        print("\n  ── Contexto SIDI_INTRADAY_V2 ──")
        df_sidi_ctx = calcular_contexto_sidi(all_prices, sp500)
        if len(df_sidi_ctx) > 0:
            df_tech = df_tech.merge(df_sidi_ctx, on="ticker", how="left")

        print("\n  ── Histórico automático SIDI_SHADOW_V1 ──")
        try:
            shadow_result = update_shadow_tracker(all_prices)
            if shadow_result.get("ok"):
                print(
                    "  ✅ Shadow V1 histórico actualizado · "
                    f"{shadow_result['signals']} señales · "
                    f"{shadow_result['open']} abiertas · "
                    f"{shadow_result['closed']} cerradas"
                )
            else:
                print(f"  ⚠ Shadow no actualizado: {shadow_result.get('reason')}")
        except Exception as e:
            print(f"  ⚠ Shadow no actualizado: {e}", flush=True)
    else:
        df_tech = pd.DataFrame()

    del all_prices
    gc.collect()
    _log_memoria("PASO 4 + contexto SIDI + liberación de precios")

    if not args.solo_tech and not args.solo_fund:
        print("\n" + "━" * 50)
        print("PASO 5/7 · Earnings dates")
        df_earn = calcular_earnings(sp500["ticker"].tolist())
    else:
        df_earn = pd.DataFrame()
    gc.collect()
    _log_memoria("PASO 5")

    if not args.solo_tech and not args.solo_earn:
        print("\n" + "━" * 50)
        print("PASO 6/7 · Fundamentales")
        df_fund = calcular_fundamentales(sp500["ticker"].tolist(), cfg)
    else:
        df_fund = pd.DataFrame()
    gc.collect()
    _log_memoria("PASO 6")

    print("\n" + "━" * 50)
    print("PASO 7/7 · Scoring y exportación")
    ruta = exportar_csv(sp500, df_tech, df_fund, df_earn, market_ctx, cfg)
    _log_memoria("PASO 7 (final)")

    print(f"""
╔══════════════════════════════════════════════╗
║  ✅ FASE A COMPLETADA                        ║
╠══════════════════════════════════════════════╣
║  Mercado  : {market_ctx['regime_icon']} {market_ctx['market_regime']:<29}║
║  Archivo  : {ruta.name[:38]:<38}║
║  Fin      : {datetime.now().strftime('%H:%M:%S'):<38}║
╚══════════════════════════════════════════════╝
""")


if __name__ == "__main__":
    main()
