"""Score fundamental normalizado por sector + Warning Signs.

SIDI_INTRADAY_V2
---------------
``full_setup`` representa la estrategia congelada tras la validación 2023-2026:
- DD60 >= 12% (sobre máximo de cierre de 60 sesiones)
- RSI14 < 40
- histograma MACD mejorando vs sesión anterior
- volumen medio 5d < volumen medio 60d
- fund_score live >= 6.5
- SPY20 <= +1%
- AbnormalReturn20 <= -10%

La selección anterior se conserva en ``legacy_full_setup`` sólo para auditoría.
"""

import sys
from pathlib import Path
import numpy as np
import pandas as pd

_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

try:
    from sector_context import get_sector_context
except ImportError as e:
    print(f"  ⚠ No se pudo importar sector_context.py ({e}) — sector_context quedará vacío en el CSV")
    def get_sector_context(ticker, sector):
        return {"sector_etf": None, "subsector": None, "peer_group": [], "critical_macro_variables": []}

try:
    from sidi_trade_plan import add_indicative_trade_plan
except ImportError:
    def add_indicative_trade_plan(df):
        return df

SIDI_STRATEGY_VERSION = "SIDI_INTRADAY_V2"
SIDI_MIN_FUND_SCORE = 6.5
SIDI_MAX_SPY20 = 1.0
SIDI_MAX_ABNORMAL20 = -10.0

SCORE_COLS = ["revenue_growth", "eps_growth", "roe", "debt_equity", "pe", "pb", "current_ratio"]


def _norm(val, med, higher_better=True, scale=2.0) -> float:
    if pd.isna(val) or pd.isna(med) or med == 0:
        return 5.0
    ratio = val / med if higher_better else (med / val if val != 0 else 2.0)
    return float(np.clip(5 + (ratio - 1) * scale * 3, 0, 10))


def _sector_medians(df: pd.DataFrame) -> dict:
    meds = {}
    for sec, grp in df.groupby("sector"):
        meds[sec] = {c: grp[c].median() for c in SCORE_COLS if c in grp.columns and not grp[c].isna().all()}
    meds["ALL"] = {c: df[c].median() for c in SCORE_COLS if c in df.columns}
    return meds


def _fund_score(row: pd.Series, sm: dict) -> pd.Series:
    sec = row.get("sector", "ALL")
    m = sm.get(sec, sm.get("ALL", {}))
    rev_g = row.get("revenue_growth", np.nan)
    eps_g = row.get("eps_growth", np.nan)
    g = _norm(rev_g, m.get("revenue_growth", 0.08)) * 0.5 + _norm(eps_g, m.get("eps_growth", 0.10)) * 0.5

    roe = row.get("roe", np.nan)
    de = row.get("debt_equity", np.nan)
    cr = row.get("current_ratio", np.nan)
    fcni = row.get("fcf_ni_ratio", np.nan)
    sf = 8.0 if (not pd.isna(fcni) and fcni >= 0.75) else 3.0 if (not pd.isna(fcni) and fcni < 0.5) else 5.0
    s = (_norm(roe, m.get("roe", 0.15), True, 1.5) * 0.35 +
         _norm(de, m.get("debt_equity", 50.0), False, 1.0) * 0.25 +
         sf * 0.25 + _norm(cr, 1.2, True, 0.8) * 0.15)

    pe = row.get("pe", np.nan)
    pb = row.get("pb", np.nan)
    fcf = row.get("fcf", np.nan)
    mc = row.get("market_cap", np.nan)
    fcf_yield = (fcf / mc) if (not pd.isna(fcf) and not pd.isna(mc) and mc > 0) else np.nan
    v_pe = _norm(pe, m.get("pe", 25.0), False, 1.0)
    v_fcf = _norm(fcf_yield, 0.03, True, 2.0) if not pd.isna(fcf_yield) else 5.0
    v_pb = _norm(pb, m.get("pb", 4.0), False, 0.8)
    v = v_pe * 0.40 + v_fcf * 0.40 + v_pb * 0.20
    total = g * 0.35 + s * 0.35 + v * 0.30
    return pd.Series({
        "fund_score": round(float(np.clip(total, 0, 10)), 2),
        "fund_growth": round(float(np.clip(g, 0, 10)), 2),
        "fund_solidity": round(float(np.clip(s, 0, 10)), 2),
        "fund_valuation": round(float(np.clip(v, 0, 10)), 2),
        "fcf_yield_calc": round(float(fcf_yield), 4) if not pd.isna(fcf_yield) else np.nan,
    })


def _warnings(row: pd.Series) -> str:
    w = []
    sec = row.get("sector", "")
    fcni = row.get("fcf_ni_ratio", np.nan)
    de = row.get("debt_equity", np.nan)
    rg = row.get("revenue_growth", np.nan)
    roe = row.get("roe", np.nan)
    if not pd.isna(fcni) and fcni < 0.75: w.append(f"FCF/NI:{fcni:.2f}")
    de_th = 150.0 if sec in ("Financials", "Utilities") else 80.0
    if not pd.isna(de) and de > de_th: w.append(f"D/E:{de:.0f}")
    if not pd.isna(rg) and rg < -0.10: w.append(f"RevG:{rg*100:.1f}%")
    if not pd.isna(roe) and roe < 0: w.append(f"ROE:{roe*100:.1f}%")
    return "|".join(w) if w else "OK"


def _gate_failures(row: pd.Series) -> str:
    failures = []
    if not bool(row.get("sidi_pass_dd12", False)): failures.append("DD12")
    if not bool(row.get("sidi_pass_rsi40", False)): failures.append("RSI40")
    if not bool(row.get("sidi_pass_macd", False)): failures.append("MACD")
    if not bool(row.get("sidi_pass_volume", False)): failures.append("VOLUME")
    if not bool(row.get("sidi_pass_fund65", False)): failures.append("FUND65")
    if not bool(row.get("sidi_context_ready", False)):
        failures.append("CONTEXT_MISSING")
    else:
        if not bool(row.get("sidi_pass_spy20", False)): failures.append("SPY20")
        if not bool(row.get("sidi_pass_abnormal20", False)): failures.append("ABNORMAL20")
    return "OK" if not failures else "|".join(failures)


def calcular_scores(sp500: pd.DataFrame, df_tech: pd.DataFrame, df_fund: pd.DataFrame) -> pd.DataFrame:
    df_fm = df_fund.merge(sp500[["ticker", "sector"]], on="ticker", how="left")
    sm = _sector_medians(df_fm)
    scores = df_fm.apply(lambda r: _fund_score(r, sm), axis=1)
    df_fm = pd.concat([df_fm, scores], axis=1)
    df_fm["warnings"] = df_fm.apply(_warnings, axis=1)
    df_fm["warning_count"] = df_fm["warnings"].apply(lambda w: 0 if w == "OK" else len(w.split("|")))

    df = sp500.copy()
    if len(df_tech) > 0: df = df.merge(df_tech, on="ticker", how="left")
    if len(df_fm) > 0: df = df.merge(df_fm.drop(columns=["sector"], errors="ignore"), on="ticker", how="left")

    df["combined_score"] = (
        df.get("fund_score", pd.Series(5.0, index=df.index)).fillna(5) * 0.60 +
        df.get("tech_score", pd.Series(5.0, index=df.index)).fillna(5) * 0.40
    ).round(2)

    df["sidi_strategy_version"] = SIDI_STRATEGY_VERSION
    for col, default in [
        ("setup_hot", False), ("setup_hot_legacy", False), ("sidi_context_ready", False),
        ("sidi_pass_dd12", False), ("sidi_pass_rsi40", False),
        ("sidi_pass_macd", False), ("sidi_pass_volume", False),
    ]:
        if col not in df.columns: df[col] = default
    if "spy_return_20d" not in df.columns: df["spy_return_20d"] = np.nan
    if "abnormal_return_20d" not in df.columns: df["abnormal_return_20d"] = np.nan

    df["sidi_pass_fund65"] = df.get("fund_score", pd.Series(np.nan, index=df.index)) >= SIDI_MIN_FUND_SCORE
    df["sidi_pass_spy20"] = (
        df["sidi_context_ready"].fillna(False).astype(bool) &
        (pd.to_numeric(df["spy_return_20d"], errors="coerce") <= SIDI_MAX_SPY20)
    )
    df["sidi_pass_abnormal20"] = (
        df["sidi_context_ready"].fillna(False).astype(bool) &
        (pd.to_numeric(df["abnormal_return_20d"], errors="coerce") <= SIDI_MAX_ABNORMAL20)
    )
    df["sidi_pass_technical"] = df["setup_hot"].fillna(False).astype(bool)
    df["full_setup"] = (
        df["sidi_pass_technical"] & df["sidi_pass_fund65"] &
        df["sidi_pass_spy20"] & df["sidi_pass_abnormal20"]
    )
    df["sidi_full_setup"] = df["full_setup"]
    df["sidi_gate_failures"] = df.apply(_gate_failures, axis=1)

    legacy_sector_ok = ~df.get("sector", pd.Series("", index=df.index)).isin(["Financials", "Communication Services"])
    df["legacy_full_setup"] = (
        (df.get("fund_score", pd.Series(np.nan, index=df.index)) >= 6.5) &
        df["setup_hot_legacy"].fillna(False).astype(bool) & legacy_sector_ok
    )

    def horizon(row):
        if pd.isna(row.get("rsi_14")): return "N/A"
        if bool(row.get("full_setup", False)): return "SIDI_INTRADAY_V2: max 7 sesiones"
        if row["rsi_14"] < 30 and row.get("near_support") and row.get("macd_improving"): return "5-10d"
        bias = row.get("trend_bias", "")
        return "10-18d" if bias == "ALCISTA" else ("3-7d" if bias == "BAJISTA" else "7-15d")
    df["horizon"] = df.apply(horizon, axis=1)

    def _sector_ctx(row):
        ctx = get_sector_context(row.get("ticker", ""), row.get("sector", ""))
        return pd.Series({
            "sector_etf": ctx["sector_etf"],
            "subsector": ctx["subsector"],
            "peer_group": "|".join(ctx["peer_group"]) if ctx["peer_group"] else "",
            "critical_macro_variables": "|".join(ctx["critical_macro_variables"]) if ctx["critical_macro_variables"] else "",
        })
    df = pd.concat([df, df.apply(_sector_ctx, axis=1)], axis=1)
    df = add_indicative_trade_plan(df)
    df = df.sort_values(["full_setup", "combined_score"], ascending=[False, False]).reset_index(drop=True)

    setups = int(df["full_setup"].sum())
    legacy_setups = int(df["legacy_full_setup"].sum())
    print(f"  ✅ Scoring completado · {SIDI_STRATEGY_VERSION}")
    print(f"     Score combinado medio : {df['combined_score'].mean():.2f}/10")
    print(f"     Fund score medio      : {df['fund_score'].mean():.2f}/10")
    print(f"     FULL Intraday V2        : {setups}")
    print(f"     FULL legacy (audit)   : {legacy_setups}")
    cols = ["ticker", "sector", "fund_score", "combined_score", "drawdown_60d", "rsi_14", "spy_return_20d", "abnormal_return_20d"]
    cols = [c for c in cols if c in df.columns]
    if setups > 0:
        print("\n  📊 FULL SIDI_INTRADAY_V2:")
        print(df[df["full_setup"] == True][cols].head(10).to_string(index=False))
    else:
        print("\n  📊 Sin FULL hoy · Top 10 por score combinado:")
        print(df[cols].head(10).to_string(index=False))
    return df
