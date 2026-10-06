"""Seguimiento contrafactual de todas las FULL SIDI_INTRADAY_V2.

La entrada es la primera vela de cinco minutos posterior al timestamp del
análisis. El motor conserva marcas diarias, MFE/MAE y resuelve TP1/TP2/SL/T7
sin depender de que exista una ejecución LIVE.
"""
from __future__ import annotations

import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from modules.control_center_v7 import ensure_schema


NEW_YORK = ZoneInfo("America/New_York")
STOP_PCT = 0.05
TP1_ATR_MULT = 1.0
TP2_ATR_MULT = 1.5
TIME_STOP_SESSIONS = 7
RISK_EUR = 150.0
COST_PCT_RT = 0.001


SHADOW_COLUMNS = [
    "episode_id", "strategy_version", "ticker", "status",
    "original_verdict", "latest_verdict", "analysis_completed_at",
    "entry_rule", "entry_date", "entry_ts", "entry_price_usd",
    "entry_price_eur", "fx_usd_per_eur", "atr14_usd", "atr14_eur",
    "stop_price_usd", "tp1_price_usd", "tp2_price_usd",
    "remaining_stop_usd", "shares", "risk_eur", "sessions_held",
    "tp1_hit", "tp1_date", "exit_date", "exit_ts",
    "exit_price_usd", "exit_reason", "pnl_pct_net", "pnl_eur_net",
    "r_multiple_net", "mfe_pct", "mae_pct", "last_mark_date",
    "last_processed_ts", "invalid_reason", "created_at", "updated_at",
]


def _iso(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value or "")


def _parse_timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _turso_conn():
    url = os.environ.get("TURSO_DATABASE_URL", "")
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if not url or not token:
        return None
    import libsql
    return libsql.connect(database=url, auth_token=token)


def _default_price_loader(tickers):
    import yfinance as yf

    result = {}
    for ticker in tickers:
        frame = yf.Ticker(ticker.replace(".", "-")).history(
            period="1mo", interval="5m", auto_adjust=False, prepost=False
        )
        bars = []
        if frame is not None and not frame.empty:
            for index, row in frame.dropna(subset=["Open", "High", "Low", "Close"]).iterrows():
                timestamp = index.to_pydatetime()
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=NEW_YORK)
                bars.append({
                    "ts": timestamp.astimezone(timezone.utc),
                    "open": float(row["Open"]), "high": float(row["High"]),
                    "low": float(row["Low"]), "close": float(row["Close"]),
                })
        result[ticker] = bars
    return result


def _position_pnl(shadow, exit_price, exit_fraction=1.0):
    entry = float(shadow["entry_price_usd"])
    fx = float(shadow.get("fx_usd_per_eur") or 1.0)
    shares = int(shadow.get("shares") or 0)
    return (exit_price - entry) / fx * shares * exit_fraction


def _close(shadow, timestamp, price, reason):
    tp1_hit = bool(shadow.get("tp1_hit"))
    if tp1_hit:
        first_half = _position_pnl(shadow, shadow["tp1_price_usd"], 0.5)
        second_half = _position_pnl(shadow, price, 0.5)
        gross_eur = first_half + second_half
        gross_pct = (
            0.5 * (shadow["tp1_price_usd"] - shadow["entry_price_usd"]) / shadow["entry_price_usd"]
            + 0.5 * (price - shadow["entry_price_usd"]) / shadow["entry_price_usd"]
        ) * 100
    else:
        gross_eur = _position_pnl(shadow, price)
        gross_pct = (price - shadow["entry_price_usd"]) / shadow["entry_price_usd"] * 100
    notional_eur = shadow["entry_price_eur"] * shadow["shares"]
    costs = notional_eur * COST_PCT_RT
    pnl_eur = gross_eur - costs
    pnl_pct_net = gross_pct - COST_PCT_RT * 100
    shadow.update({
        "status": "CLOSED", "exit_date": timestamp.astimezone(NEW_YORK).date().isoformat(),
        "exit_ts": _iso(timestamp), "exit_price_usd": price,
        "exit_reason": reason, "pnl_pct_net": pnl_pct_net,
        "pnl_eur_net": pnl_eur,
        "r_multiple_net": pnl_eur / float(shadow.get("risk_eur") or RISK_EUR),
    })


def replay_shadow(shadow, bars, as_of=None):
    """Reproduce una Shadow usando velas UTC ordenadas; función pura testeable."""
    bars = sorted(bars, key=lambda item: item["ts"])
    completed = _parse_timestamp(shadow["analysis_completed_at"])
    analysis_day = completed.astimezone(NEW_YORK).date()
    eligible = [bar for bar in bars if bar["ts"] > completed]
    if not shadow.get("entry_price_usd"):
        entry_bar = next(
            (bar for bar in eligible if bar["ts"].astimezone(NEW_YORK).date() == analysis_day),
            None,
        )
        if not entry_bar:
            as_of = as_of or (bars[-1]["ts"] if bars else None)
            if as_of and _parse_timestamp(as_of).astimezone(NEW_YORK).date() > analysis_day:
                shadow.update({
                    "status": "INVALID_NO_ENTRY",
                    "invalid_reason": "NO_5M_BAR_AFTER_ANALYSIS_SAME_SESSION",
                })
            return shadow, []
        entry = entry_bar["open"]
        fx = float(shadow.get("fx_usd_per_eur") or 1.0)
        entry_eur = entry / fx
        stop = entry * (1 - STOP_PCT)
        risk_per_share_eur = (entry - stop) / fx
        shares = math.floor(RISK_EUR / risk_per_share_eur) if risk_per_share_eur else 0
        if shares < 1:
            shadow.update({"status": "INVALID_SIZING", "invalid_reason": "SHARES_LT_ONE"})
            return shadow, []
        shadow.update({
            "status": "OPEN", "entry_date": analysis_day.isoformat(),
            "entry_ts": _iso(entry_bar["ts"]), "entry_price_usd": entry,
            "entry_price_eur": entry_eur, "stop_price_usd": stop,
            "tp1_price_usd": entry + TP1_ATR_MULT * shadow["atr14_usd"],
            "tp2_price_usd": entry + TP2_ATR_MULT * shadow["atr14_usd"],
            "remaining_stop_usd": stop, "shares": shares,
            "risk_eur": shares * risk_per_share_eur,
            "mfe_pct": 0.0, "mae_pct": 0.0,
        })
        bars = [bar for bar in eligible if bar["ts"] >= entry_bar["ts"]]
    else:
        entry_ts = _parse_timestamp(shadow["entry_ts"])
        cutoff = _parse_timestamp(shadow["last_processed_ts"]) if shadow.get("last_processed_ts") else entry_ts
        bars = [bar for bar in bars if bar["ts"] > cutoff]

    if not bars:
        return shadow, []

    entry = float(shadow["entry_price_usd"])
    by_day = defaultdict(list)
    for bar in bars:
        by_day[bar["ts"].astimezone(NEW_YORK).date().isoformat()].append(bar)
    marks = []
    session_dates = sorted(by_day)
    previous_sessions = int(shadow.get("sessions_held") or 0)
    previous_mark_date = shadow.get("last_mark_date")
    for day in session_dates:
        if shadow.get("status") == "CLOSED":
            break
        day_bars = sorted(by_day[day], key=lambda item: item["ts"])
        if day != previous_mark_date:
            previous_sessions += 1
        shadow["sessions_held"] = previous_sessions
        day_high = max(bar["high"] for bar in day_bars)
        day_low = min(bar["low"] for bar in day_bars)
        path_high = entry
        path_low = entry
        touched = {"sl": False, "tp1": False, "tp2": False}
        for bar_index, bar in enumerate(day_bars):
            path_high = max(path_high, bar["high"])
            path_low = min(path_low, bar["low"])
            shadow["mfe_pct"] = max(
                float(shadow.get("mfe_pct") or 0), (path_high - entry) / entry * 100
            )
            shadow["mae_pct"] = min(
                float(shadow.get("mae_pct") or 0), (path_low - entry) / entry * 100
            )
            stop = float(shadow.get("remaining_stop_usd") or shadow["stop_price_usd"])
            is_session_open = bar_index == 0 and day != shadow.get("entry_date")
            if is_session_open and bar["open"] <= stop:
                touched["sl"] = True
                _close(shadow, bar["ts"], bar["open"], "GAP_SL")
                break
            if bar["low"] <= stop:
                touched["sl"] = True
                _close(shadow, bar["ts"], stop, "BE" if shadow.get("tp1_hit") else "SL")
                break
            if not shadow.get("tp1_hit") and bar["high"] >= shadow["tp1_price_usd"]:
                touched["tp1"] = True
                shadow.update({
                    "status": "TP1", "tp1_hit": 1, "tp1_date": day,
                    "tp1_price_usd": shadow["tp1_price_usd"],
                    "remaining_stop_usd": entry,
                })
                if bar["low"] <= entry:
                    _close(shadow, bar["ts"], entry, "TP1_BE")
                    break
            if shadow.get("tp1_hit") and bar["high"] >= shadow["tp2_price_usd"]:
                touched["tp2"] = True
                _close(shadow, bar["ts"], shadow["tp2_price_usd"], "TP2")
                break
        if shadow.get("status") != "CLOSED" and previous_sessions >= TIME_STOP_SESSIONS:
            _close(shadow, day_bars[-1]["ts"], day_bars[-1]["close"], "T7")
        marks.append({
            "episode_id": shadow["episode_id"], "session_date": day,
            "open": day_bars[0]["open"], "high": day_high, "low": day_low,
            "close": day_bars[-1]["close"], "mfe_pct": shadow["mfe_pct"],
            "mae_pct": shadow["mae_pct"], "touched_sl": int(touched["sl"]),
            "touched_tp1": int(touched["tp1"]), "touched_tp2": int(touched["tp2"]),
            "source": "YFINANCE_5M",
        })
        shadow["last_mark_date"] = day
        shadow["last_processed_ts"] = _iso(day_bars[-1]["ts"])
        previous_mark_date = day
    return shadow, marks


def _persist(conn, shadow, marks, now):
    fields = [column for column in SHADOW_COLUMNS if column not in {"episode_id", "created_at"}]
    shadow["updated_at"] = _iso(now)
    conn.execute(
        f"UPDATE sidi_shadow_trades_v2 SET {', '.join(f'{field}=?' for field in fields)} "
        "WHERE episode_id=?",
        (*[shadow.get(field) for field in fields], shadow["episode_id"]),
    )
    for mark in marks:
        conn.execute("""
            INSERT INTO sidi_shadow_marks_v2
            (episode_id, session_date, open, high, low, close, mfe_pct, mae_pct,
             touched_sl, touched_tp1, touched_tp2, source, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(episode_id, session_date) DO UPDATE SET
              open=excluded.open, high=excluded.high, low=excluded.low,
              close=excluded.close, mfe_pct=excluded.mfe_pct,
              mae_pct=excluded.mae_pct, touched_sl=excluded.touched_sl,
              touched_tp1=excluded.touched_tp1, touched_tp2=excluded.touched_tp2,
              source=excluded.source, updated_at=excluded.updated_at
        """, (
            mark["episode_id"], mark["session_date"], mark["open"], mark["high"],
            mark["low"], mark["close"], mark["mfe_pct"], mark["mae_pct"],
            mark["touched_sl"], mark["touched_tp1"], mark["touched_tp2"],
            mark["source"], _iso(now),
        ))
    if shadow["status"] == "CLOSED":
        conn.execute(
            "UPDATE sidi_setup_episodes SET status='CLOSED', closed_at=?, updated_at=? "
            "WHERE episode_id=?",
            (shadow["exit_ts"], _iso(now), shadow["episode_id"]),
        )
    elif str(shadow["status"]).startswith("INVALID"):
        conn.execute(
            "UPDATE sidi_setup_episodes SET status=?, closed_at=?, updated_at=? "
            "WHERE episode_id=?",
            (shadow["status"], _iso(now), _iso(now), shadow["episode_id"]),
        )
    elif shadow["status"] in {"OPEN", "TP1"}:
        conn.execute(
            "UPDATE sidi_setup_episodes SET status=?, updated_at=? WHERE episode_id=?",
            (shadow["status"], _iso(now), shadow["episode_id"]),
        )


def update_intraday_shadow_tracker(price_loader=None, conn=None, now=None):
    conn = conn or _turso_conn()
    if conn is None:
        return {"ok": False, "reason": "turso_not_configured"}
    ensure_schema(conn)
    now = now or datetime.now(timezone.utc)
    placeholders = ",".join("?" for _ in ("PENDING_ENTRY", "OPEN", "TP1"))
    rows = conn.execute(
        f"SELECT {', '.join(SHADOW_COLUMNS)} FROM sidi_shadow_trades_v2 "
        f"WHERE status IN ({placeholders}) ORDER BY analysis_completed_at",
        ("PENDING_ENTRY", "OPEN", "TP1"),
    ).fetchall()
    shadows = [dict(zip(SHADOW_COLUMNS, row)) for row in rows]
    if not shadows:
        return {"ok": True, "tracked": 0, "open": 0, "closed": 0}
    loader = price_loader or _default_price_loader
    prices = loader(sorted({shadow["ticker"] for shadow in shadows}))
    closed = 0
    for shadow in shadows:
        replayed, marks = replay_shadow(
            shadow, prices.get(shadow["ticker"], []), as_of=now
        )
        _persist(conn, replayed, marks, now)
        closed += replayed["status"] == "CLOSED"
    conn.commit()
    statuses = [shadow["status"] for shadow in shadows]
    return {
        "ok": True, "tracked": len(shadows),
        "open": sum(status in {"OPEN", "TP1"} for status in statuses),
        "closed": closed,
    }
