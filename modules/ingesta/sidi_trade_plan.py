"""Operational trade-plan fields for SIDI_INTRADAY_V2.

Execution model:
    signal: last completed US session close
    analysis: same afternoon, after the NYSE open
    entry reference: live quote captured after the qualitative verdict
    official entry: actual broker fill, never a retroactive opening price
    TP1: entry + 1.0 * ATR(14) measured on signal day; sell 50%
    TP2: entry + 1.5 * ATR(14) measured on signal day; sell the remainder
    after TP1: move the remaining stop to break-even
    SL: entry * 0.95
    time-stop: 7 trading sessions
    risk: 1.5% of realised portfolio capital
    max simultaneous positions: 3
    gap below SL: exit at actual opening price
    TP+SL touched same bar: conservative SL-first
"""
from __future__ import annotations

import math
import numpy as np
import pandas as pd

STRATEGY_VERSION = "SIDI_INTRADAY_V2"
TP1_ATR_MULT = 1.0
TP2_ATR_MULT = 1.5
STOP_PCT = 0.05
TIME_STOP_SESSIONS = 7
RISK_PCT = 0.015
MAX_POSITIONS = 3
VALIDATION_COST_BPS_RT = 10


def _base_metadata() -> dict:
    return {
        "sidi_strategy_version": STRATEGY_VERSION,
        "sidi_entry_rule": "POST_ANALYSIS_ACTUAL_FILL",
        "sidi_entry_reference_source": "LIVE_QUOTE_AT_ANALYSIS",
        "sidi_actual_fill_required": True,
        "sidi_tp1_atr_mult": TP1_ATR_MULT,
        "sidi_tp2_atr_mult": TP2_ATR_MULT,
        "sidi_tp_atr_mult": TP1_ATR_MULT,
        "sidi_sl_pct": round(-STOP_PCT * 100.0, 4),
        "sidi_time_stop_sessions": TIME_STOP_SESSIONS,
        "sidi_risk_pct": round(RISK_PCT * 100.0, 4),
        "sidi_max_positions": MAX_POSITIONS,
        "sidi_partial_exit_rule": "SELL_50_PCT_AT_TP1_REST_AT_TP2",
        "sidi_after_tp1_stop_rule": "MOVE_REMAINDER_TO_BREAK_EVEN",
        "sidi_gap_stop_rule": "EXIT_AT_OPEN_IF_OPEN_BELOW_SL",
        "sidi_intraday_conflict_rule": "SL_FIRST",
        "sidi_validation_cost_bps_rt": VALIDATION_COST_BPS_RT,
    }


def plan_from_entry(entry: float, atr14: float, capital: float | None = None) -> dict:
    entry = float(entry)
    atr14 = float(atr14)
    if not math.isfinite(entry) or entry <= 0:
        raise ValueError("entry must be a finite positive number")
    if not math.isfinite(atr14) or atr14 <= 0:
        raise ValueError("atr14 must be a finite positive number")
    tp1 = entry + TP1_ATR_MULT * atr14
    tp2 = entry + TP2_ATR_MULT * atr14
    sl = entry * (1.0 - STOP_PCT)
    out = _base_metadata()
    out.update({
        "sidi_entry_status": "ENTERED",
        "sidi_entry_price": round(entry, 4),
        "sidi_atr14_signal": round(atr14, 4),
        "sidi_tp1_price": round(tp1, 4),
        "sidi_tp2_price": round(tp2, 4),
        "sidi_tp_price": round(tp1, 4),
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
    exact = plan_from_entry(signal_close, atr14)
    out = _base_metadata()
    out.update({
        "sidi_entry_status": "PENDING_ANALYSIS",
        "sidi_atr14_signal": exact["sidi_atr14_signal"],
        "sidi_tp1_offset_abs": round(TP1_ATR_MULT * float(atr14), 4),
        "sidi_tp2_offset_abs": round(TP2_ATR_MULT * float(atr14), 4),
        "sidi_tp1_indicative_from_close": exact["sidi_tp1_price"],
        "sidi_tp2_indicative_from_close": exact["sidi_tp2_price"],
        "sidi_sl_indicative_from_close": exact["sidi_sl_price"],
        "sidi_plan_note": (
            "Indicative levels use the signal close only. Final SL/TP1/TP2 and sizing "
            "must be recalculated from the actual post-analysis broker fill."
        ),
    })
    return out


def add_indicative_trade_plan(df: pd.DataFrame) -> pd.DataFrame:
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
    if "sidi_strategy_version" in plan_df.columns and "sidi_strategy_version" in out.columns:
        plan_df = plan_df.drop(columns=["sidi_strategy_version"])
    return pd.concat([out, plan_df], axis=1)
