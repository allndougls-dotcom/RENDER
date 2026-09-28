"""Operational trade-plan fields for SIDI_SHADOW_V1.

Frozen execution after robustness validation:

    signal: close T
    entry: next session OPEN (T+1)
    TP: entry + 0.75 * ATR(14) measured on signal day
    SL: entry * 0.95
    time-stop: 7 trading sessions
    risk: 1.5% of realised portfolio capital
    max simultaneous positions: 5
    gap below SL: exit at actual opening price
    TP+SL touched same bar: conservative SL-first
    validation costs: 10 bps round trip

Before T+1 opens, the exact entry/TP/SL cannot be known. Close-based fields are
therefore explicitly labelled as indications, not executable exact prices.
"""
from __future__ import annotations

import math
import numpy as np
import pandas as pd

STRATEGY_VERSION = "SIDI_SHADOW_V1"
TP_ATR_MULT = 0.75
STOP_PCT = 0.05
TIME_STOP_SESSIONS = 7
RISK_PCT = 0.015
MAX_POSITIONS = 5
VALIDATION_COST_BPS_RT = 10


def _base_metadata() -> dict:
    return {
        "sidi_strategy_version": STRATEGY_VERSION,
        "sidi_entry_rule": "NEXT_SESSION_OPEN",
        "sidi_tp_atr_mult": TP_ATR_MULT,
        "sidi_sl_pct": round(-STOP_PCT * 100.0, 4),
        "sidi_time_stop_sessions": TIME_STOP_SESSIONS,
        "sidi_risk_pct": round(RISK_PCT * 100.0, 4),
        "sidi_max_positions": MAX_POSITIONS,
        "sidi_gap_stop_rule": "EXIT_AT_OPEN_IF_OPEN_BELOW_SL",
        "sidi_intraday_conflict_rule": "SL_FIRST",
        "sidi_validation_cost_bps_rt": VALIDATION_COST_BPS_RT,
    }


def plan_from_entry(entry: float, atr14: float, capital: float | None = None) -> dict:
    """Exact plan once the T+1 opening/entry price is known."""
    entry = float(entry)
    atr14 = float(atr14)
    if not math.isfinite(entry) or entry <= 0:
        raise ValueError("entry must be a finite positive number")
    if not math.isfinite(atr14) or atr14 <= 0:
        raise ValueError("atr14 must be a finite positive number")

    tp = entry + TP_ATR_MULT * atr14
    sl = entry * (1.0 - STOP_PCT)
    tp_pct = (tp / entry - 1.0) * 100.0

    out = _base_metadata()
    out.update({
        "sidi_entry_status": "ENTERED",
        "sidi_entry_price": round(entry, 4),
        "sidi_atr14_signal": round(atr14, 4),
        "sidi_tp_price": round(tp, 4),
        "sidi_tp_pct": round(tp_pct, 4),
        "sidi_sl_price": round(sl, 4),
    })

    if capital is not None and math.isfinite(float(capital)) and float(capital) > 0:
        risk_eur = float(capital) * RISK_PCT
        stop_distance = entry - sl
        shares = math.floor(risk_eur / stop_distance) if stop_distance > 0 else 0
        out["sidi_risk_eur"] = round(risk_eur, 2)
        out["sidi_shares"] = int(max(shares, 0))
        out["sidi_notional"] = round(max(shares, 0) * entry, 2)
    return out


def indicative_plan(signal_close: float, atr14: float) -> dict:
    """Close-T indication shown before the exact T+1 opening price exists."""
    exact = plan_from_entry(signal_close, atr14)
    out = _base_metadata()
    out.update({
        "sidi_entry_status": "PENDING_NEXT_OPEN",
        "sidi_atr14_signal": exact["sidi_atr14_signal"],
        "sidi_tp_offset_abs": round(TP_ATR_MULT * float(atr14), 4),
        "sidi_tp_indicative_from_close": exact["sidi_tp_price"],
        "sidi_sl_indicative_from_close": exact["sidi_sl_price"],
        "sidi_tp_pct_indicative": exact["sidi_tp_pct"],
        "sidi_plan_note": "Exact TP/SL are fixed from the actual T+1 opening/entry price",
    })
    return out


def add_indicative_trade_plan(df: pd.DataFrame) -> pd.DataFrame:
    """Append explicit SIDI_SHADOW_V1 execution fields without changing selection."""
    out = df.copy()
    rows = []
    for _, row in out.iterrows():
        price = row.get("price", np.nan)
        atr = row.get("atr_14", np.nan)
        if pd.isna(price) or pd.isna(atr) or float(price) <= 0 or float(atr) <= 0:
            meta = _base_metadata()
            meta["sidi_entry_status"] = "UNAVAILABLE"
            rows.append(meta)
            continue
        rows.append(indicative_plan(float(price), float(atr)))
    plan_df = pd.DataFrame(rows, index=out.index)

    # Avoid duplicate version column when scoring already stamped it.
    dupes = [c for c in plan_df.columns if c in out.columns]
    for c in dupes:
        if c == "sidi_strategy_version":
            plan_df = plan_df.drop(columns=[c])
    return pd.concat([out, plan_df], axis=1)
