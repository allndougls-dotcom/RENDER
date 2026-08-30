"""Score fundamental normalizado por sector + Warning Signs.

Nota sobre escala yFinance:
- returnOnEquity, revenueGrowth, earningsGrowth → DECIMAL (0.15 = 15%)
- debtToEquity, trailingPE, priceToBook, currentRatio → escala normal
- freeCashflow, netIncomeToCommon → dólares absolutos
"""

import sys
from pathlib import Path
import numpy as np
import pandas as pd

# Aseguramos que la carpeta de este archivo (modules/ingesta/) esté en el path,
# independientemente de cómo el script que importa scoring.py haya configurado
# sys.path. Sin esto, "from sector_context import ..." puede fallar en silencio
# cuando scoring.py se invoca desde main_ingesta.py (que solo añade modules/,
# no modules/ingesta/), cayendo al fallback y dejando sector_context en null
# para TODAS las filas sin ningún error visible en consola.
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

try:
    from sector_context import get_sector_context
except ImportError as e:
    # Fallback si sector_context.py de verdad no está disponible (archivo
    # ausente, no solo un problema de path). Se avisa por consola para que
    # el fallo sea visible en vez de silencioso.
    print(f"  ⚠ No se pudo importar sector_context.py ({e}) — sector_context quedará vacío en el CSV")
    def get_sector_context(ticker, sector):
        return {"sector_etf": None, "subsector": None, "peer_group": [], "critical_macro_variables": []}

SCORE_COLS = ["revenue_growth", "eps_growth", "roe", "debt_equity",
              "pe", "pb", "current_ratio"]


def _norm(val, med, higher_better=True, scale=2.0) -> float:
    if pd.isna(val) or pd.isna(med) or med == 0:
        return 5.0
    ratio = val / med if higher_better else (med / val if val != 0 else 2.0)
    return float(np.clip(5 + (ratio - 1) * scale * 3, 0, 10))


def _sector_medians(df: pd.DataFrame) -> dict:
    meds = {}
    for sec, grp in df.groupby("sector"):
        meds[sec] = {c: grp[c].median() for c in SCORE_COLS
                     if c in grp.columns and not grp[c].isna().all()}
    meds["ALL"] = {c: df[c].median() for c in SCORE_COLS if c in df.columns}
    return meds


def _fund_score(row: pd.Series, sm: dict) -> pd.Series:
    sec = row.get("sector", "ALL")
    m   = sm.get(sec, sm.get("ALL", {}))

    # CRECIMIENTO (35%) — yFinance en decimal
    rev_g   = row.get("revenue_growth", np.nan)
    eps_g   = row.get("eps_growth",     np.nan)
    med_rev = m.get("revenue_growth", 0.08)
    med_eps = m.get("eps_growth",     0.10)
    g = _norm(rev_g, med_rev) * 0.5 + _norm(eps_g, med_eps) * 0.5

    # SOLIDEZ (35%)
    roe  = row.get("roe",           np.nan)  # decimal
    de   = row.get("debt_equity",   np.nan)  # normal
    cr   = row.get("current_ratio", np.nan)
    fcni = row.get("fcf_ni_ratio",  np.nan)

    med_roe = m.get("roe", 0.15)
    med_de  = m.get("debt_equity", 50.0)

    sf = (8.0 if (not pd.isna(fcni) and fcni >= 0.75)
          else 3.0 if (not pd.isna(fcni) and fcni < 0.5)
          else 5.0)

    s = (_norm(roe, med_roe, higher_better=True,  scale=1.5) * 0.35 +
         _norm(de,  med_de,  higher_better=False, scale=1.0) * 0.25 +
         sf * 0.25 +
         _norm(cr,  1.2,     higher_better=True,  scale=0.8) * 0.15)

    # VALORACIÓN (30%)
    pe  = row.get("pe",         np.nan)
    pb  = row.get("pb",         np.nan)
    fcf = row.get("fcf",        np.nan)
    mc  = row.get("market_cap", np.nan)

    med_pe = m.get("pe",  25.0)
    med_pb = m.get("pb",   4.0)

    fcf_yield = (fcf / mc) if (not pd.isna(fcf) and not pd.isna(mc) and mc > 0) else np.nan

    v_pe  = _norm(pe, med_pe, higher_better=False, scale=1.0)
    v_fcf = _norm(fcf_yield, 0.03, higher_better=True, scale=2.0) if not pd.isna(fcf_yield) else 5.0
    v_pb  = _norm(pb, med_pb, higher_better=False, scale=0.8)
    v = v_pe * 0.40 + v_fcf * 0.40 + v_pb * 0.20

    total = g * 0.35 + s * 0.35 + v * 0.30

    return pd.Series({
        "fund_score":     round(float(np.clip(total, 0, 10)), 2),
        "fund_growth":    round(float(np.clip(g,     0, 10)), 2),
        "fund_solidity":  round(float(np.clip(s,     0, 10)), 2),
        "fund_valuation": round(float(np.clip(v,     0, 10)), 2),
        "fcf_yield_calc": round(float(fcf_yield), 4) if not pd.isna(fcf_yield) else np.nan,
    })


def _warnings(row: pd.Series) -> str:
    w   = []
    sec  = row.get("sector", "")
    fcni = row.get("fcf_ni_ratio",   np.nan)
    de   = row.get("debt_equity",    np.nan)
    rg   = row.get("revenue_growth", np.nan)
    roe  = row.get("roe",            np.nan)

    if not pd.isna(fcni) and fcni < 0.75:
        w.append(f"FCF/NI:{fcni:.2f}")
    de_th = 150.0 if sec in ("Financials", "Utilities") else 80.0
    if not pd.isna(de) and de > de_th:
        w.append(f"D/E:{de:.0f}")
    if not pd.isna(rg) and rg < -0.10:
        w.append(f"RevG:{rg*100:.1f}%")
    if not pd.isna(roe) and roe < 0:
        w.append(f"ROE:{roe*100:.1f}%")
    return "|".join(w) if w else "OK"


def calcular_scores(sp500: pd.DataFrame, df_tech: pd.DataFrame,
                    df_fund: pd.DataFrame) -> pd.DataFrame:

    df_fm = df_fund.merge(sp500[["ticker", "sector"]], on="ticker", how="left")
    sm    = _sector_medians(df_fm)
    scores = df_fm.apply(lambda r: _fund_score(r, sm), axis=1)
    df_fm  = pd.concat([df_fm, scores], axis=1)

    df_fm["warnings"]      = df_fm.apply(_warnings, axis=1)
    df_fm["warning_count"] = df_fm["warnings"].apply(
        lambda w: 0 if w == "OK" else len(w.split("|")))

    df = sp500.copy()
    if len(df_tech) > 0:
        df = df.merge(df_tech, on="ticker", how="left")
    if len(df_fm) > 0:
        df = df.merge(df_fm.drop(columns=["sector"], errors="ignore"), on="ticker", how="left")

    df["combined_score"] = (
        df.get("fund_score", pd.Series(5.0, index=df.index)).fillna(5) * 0.60 +
        df.get("tech_score", pd.Series(5.0, index=df.index)).fillna(5) * 0.40
    ).round(2)

    # ── Parámetros validados por backtest (2 años, 503 tickers) + walk-forward ──
    # DD>=10% (no 12%) + fund_score>=6.5 + excluir Financials/Comm.Services
    # mejora WR 55.3%→58.4%, PF 1.42x→1.61x, Retorno +138.6%→+219.1%, MDD -23.6%→-19.6%
    SECTORES_EXCLUIDOS = ["Financials", "Communication Services"]

    if all(c in df.columns for c in ["fund_score", "drawdown_60d", "setup_hot", "sector"]):
        df["full_setup"] = (
            (df["fund_score"]   >= 6.5) &
            (df["drawdown_60d"] <= -10) &
            (df["setup_hot"]    == True) &
            (~df["sector"].isin(SECTORES_EXCLUIDOS))
        )
    elif all(c in df.columns for c in ["fund_score", "drawdown_60d", "setup_hot"]):
        # Fallback sin columna 'sector' disponible — solo filtros de score/drawdown
        df["full_setup"] = (
            (df["fund_score"]   >= 6.5) &
            (df["drawdown_60d"] <= -10) &
            (df["setup_hot"]    == True)
        )
    else:
        df["full_setup"] = False

    def horizon(row):
        if pd.isna(row.get("rsi_14")): return "N/A"
        if row["rsi_14"] < 30 and row.get("near_support") and row.get("macd_improving"): return "5-10d"
        bias = row.get("trend_bias", "")
        return "10-18d" if bias == "ALCISTA" else ("3-7d" if bias == "BAJISTA" else "7-15d")

    df["horizon"] = df.apply(horizon, axis=1)

    # ── Contexto sectorial (ETF, peers, macro vars) — para el analizador de noticias ──
    def _sector_ctx(row):
        ctx = get_sector_context(row.get("ticker", ""), row.get("sector", ""))
        return pd.Series({
            "sector_etf":               ctx["sector_etf"],
            "subsector":                ctx["subsector"],
            "peer_group":               "|".join(ctx["peer_group"]) if ctx["peer_group"] else "",
            "critical_macro_variables": "|".join(ctx["critical_macro_variables"]) if ctx["critical_macro_variables"] else "",
        })

    sector_ctx_df = df.apply(_sector_ctx, axis=1)
    df = pd.concat([df, sector_ctx_df], axis=1)

    df = df.sort_values("combined_score", ascending=False).reset_index(drop=True)

    setups = df["full_setup"].sum()
    print(f"  ✅ Scoring completado")
    print(f"     Score combinado medio : {df['combined_score'].mean():.2f}/10")
    print(f"     Fund score medio      : {df['fund_score'].mean():.2f}/10")
    print(f"     Setups completos      : {setups}")

    cols = ["ticker", "sector", "fund_score", "tech_score", "combined_score", "drawdown_60d", "rsi_14"]
    cols = [c for c in cols if c in df.columns]
    if setups > 0:
        print(f"\n  📊 Top setups:")
        print(df[df["full_setup"] == True][cols].head(10).to_string(index=False))
    else:
        print(f"\n  📊 Top 10 por score combinado:")
        print(df[cols].head(10).to_string(index=False))

    return df
