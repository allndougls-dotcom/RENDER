"""Registro automático y reproducible de SIDI_SHADOW_V1.

Usa el último CSV ya cerrado como fuente de señales y las series OHLCV de la
ingesta siguiente para fijar Open T+1 y resolver TP/SL/T7. Turso conserva las
señales; cada ejecución reconstruye la cartera Shadow cronológicamente para
respetar riesgo del 1,5 %, cinco posiciones y capital realizado.
"""
from __future__ import annotations

import csv
import json
import math
import os
from datetime import datetime
from pathlib import Path

STRATEGY_VERSION = "SIDI_SHADOW_V1"
INITIAL_CAPITAL = 10_000.0
RISK_PCT = 0.015
MAX_POSITIONS = 5
TP_ATR_MULT = 0.75
STOP_PCT = 0.05
TIME_STOP_SESSIONS = 7
COST_PCT_RT = 0.001


def _bool(value) -> bool:
    return value is True or str(value).strip().lower() in {"true", "1", "yes", "si", "sí"}


def _float(value, default=None):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _date(value) -> str:
    return str(value or "")[:10]


def _turso_conn():
    url = os.environ.get("TURSO_DATABASE_URL", "")
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if not url or not token:
        return None
    import libsql
    conn = libsql.connect(database=url, auth_token=token)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sidi_shadow_signals (
            ticker TEXT NOT NULL,
            signal_date TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            company TEXT,
            sector TEXT,
            combined_score REAL,
            fund_score REAL,
            dd60 REAL,
            rsi14 REAL,
            atr14_signal REAL NOT NULL,
            signal_close REAL,
            status TEXT NOT NULL,
            entry_date TEXT,
            entry_open REAL,
            stop_price REAL,
            target_price REAL,
            shares INTEGER,
            risk_eur REAL,
            sessions_held INTEGER,
            exit_date TEXT,
            exit_price REAL,
            exit_reason TEXT,
            pnl_pct_gross REAL,
            pnl_pct_net REAL,
            pnl_eur_net REAL,
            r_multiple_net REAL,
            capital_after REAL,
            snapshot_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (ticker, signal_date, strategy_version)
        )
    """)
    conn.commit()
    return conn


def _latest_closed_csv(data_dir: Path):
    files = sorted(data_dir.glob("sp500_full_export_*.csv"))
    return files[-1] if files else None


def _signal_date(row: dict, path: Path) -> str:
    price_date = _date(row.get("price_date"))
    if price_date:
        return price_date
    vintage = _date(row.get("data_vintage"))
    if vintage:
        return vintage
    raw = path.stem.rsplit("_", 1)[-1]
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 else datetime.now().date().isoformat()


def _load_new_signals(path: Path) -> list[dict]:
    if not path:
        return []
    out = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not _bool(row.get("full_setup")):
                continue
            atr = _float(row.get("sidi_atr14_signal") or row.get("atr_14"))
            if not atr or atr <= 0:
                continue
            out.append({
                "ticker": (row.get("ticker") or "").strip().upper(),
                "signal_date": _signal_date(row, path),
                "strategy_version": row.get("sidi_strategy_version") or STRATEGY_VERSION,
                "company": row.get("name"),
                "sector": row.get("sector"),
                "combined_score": _float(row.get("combined_score"), 0.0),
                "fund_score": _float(row.get("fund_score")),
                "dd60": abs(_float(row.get("drawdown_60d"), 0.0)),
                "rsi14": _float(row.get("rsi_14")),
                "atr14_signal": atr,
                "signal_close": _float(row.get("price")),
                "snapshot_json": json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            })
    return [x for x in out if x["ticker"]]


def _insert_signals(conn, signals: list[dict]):
    now = datetime.now().isoformat()
    for s in signals:
        active = conn.execute(
            "SELECT 1 FROM sidi_shadow_signals WHERE ticker=? AND strategy_version=? "
            "AND status IN ('WAITING_ENTRY','OPEN') LIMIT 1",
            (s["ticker"], STRATEGY_VERSION),
        ).fetchone()
        if active:
            continue
        conn.execute("""
            INSERT OR IGNORE INTO sidi_shadow_signals
            (ticker, signal_date, strategy_version, company, sector,
             combined_score, fund_score, dd60, rsi14, atr14_signal,
             signal_close, status, snapshot_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'WAITING_ENTRY', ?, ?, ?)
        """, (
            s["ticker"], s["signal_date"], STRATEGY_VERSION, s["company"], s["sector"],
            s["combined_score"], s["fund_score"], s["dd60"], s["rsi14"], s["atr14_signal"],
            s["signal_close"], s["snapshot_json"], now, now,
        ))
    conn.commit()


def _load_signals(conn) -> list[dict]:
    columns = [
        "ticker", "signal_date", "strategy_version", "company", "sector",
        "combined_score", "fund_score", "dd60", "rsi14", "atr14_signal",
        "signal_close", "snapshot_json",
    ]
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM sidi_shadow_signals "
        "WHERE strategy_version=? ORDER BY signal_date, combined_score DESC, ticker",
        (STRATEGY_VERSION,),
    ).fetchall()
    return [dict(zip(columns, row)) for row in rows]


def _bars_by_ticker(all_prices: dict) -> dict[str, dict[str, dict]]:
    result = {}
    for ticker, df in all_prices.items():
        bars = {}
        for idx, row in df.iterrows():
            day = str(getattr(idx, "date", lambda: idx)())[:10]
            bars[day] = {
                "open": _float(row.get("Open")),
                "high": _float(row.get("High")),
                "low": _float(row.get("Low")),
                "close": _float(row.get("Close")),
            }
        result[ticker.upper()] = {k: v for k, v in bars.items() if all(x is not None for x in v.values())}
    return result


def _replay(signals: list[dict], prices: dict[str, dict[str, dict]]) -> list[dict]:
    for s in signals:
        dates = sorted(d for d in prices.get(s["ticker"], {}) if d > s["signal_date"])
        s.update({
            "status": "WAITING_ENTRY", "entry_date": dates[0] if dates else None,
            "entry_open": None, "stop_price": None, "target_price": None,
            "shares": None, "risk_eur": None, "sessions_held": 0,
            "exit_date": None, "exit_price": None, "exit_reason": None,
            "pnl_pct_gross": None, "pnl_pct_net": None, "pnl_eur_net": None,
            "r_multiple_net": None, "capital_after": None,
        })
    dates = sorted({d for bars in prices.values() for d in bars})
    open_positions = []
    capital = INITIAL_CAPITAL
    waiting_by_date = {}
    for s in signals:
        if s["entry_date"]:
            waiting_by_date.setdefault(s["entry_date"], []).append(s)

    def close_position(s, day, price, reason):
        nonlocal capital
        gross = (price - s["entry_open"]) / s["entry_open"]
        net = gross - COST_PCT_RT
        pnl = s["shares"] * s["entry_open"] * net
        capital = max(1.0, capital + pnl)
        s.update({
            "status": "CLOSED", "exit_date": day, "exit_price": price,
            "exit_reason": reason, "pnl_pct_gross": gross * 100,
            "pnl_pct_net": net * 100, "pnl_eur_net": pnl,
            "r_multiple_net": net / STOP_PCT, "capital_after": capital,
        })

    for day in dates:
        survivors = []
        for s in open_positions:
            bar = prices.get(s["ticker"], {}).get(day)
            if not bar:
                survivors.append(s)
                continue
            s["sessions_held"] += 1
            if bar["open"] <= s["stop_price"]:
                close_position(s, day, bar["open"], "GAP_SL")
            elif bar["low"] <= s["stop_price"]:
                close_position(s, day, s["stop_price"], "SL")
            elif bar["high"] >= s["target_price"]:
                close_position(s, day, s["target_price"], "TP")
            elif s["sessions_held"] >= TIME_STOP_SESSIONS:
                close_position(s, day, bar["close"], "T7")
            else:
                survivors.append(s)
        open_positions = survivors

        candidates = sorted(waiting_by_date.get(day, []), key=lambda x: (-float(x["combined_score"] or 0), x["ticker"]))
        for s in candidates:
            if len(open_positions) >= MAX_POSITIONS:
                s["status"] = "SKIPPED_NO_SLOT"
                continue
            bar = prices.get(s["ticker"], {}).get(day)
            if not bar:
                continue
            entry = bar["open"]
            stop = entry * (1 - STOP_PCT)
            risk = capital * RISK_PCT
            shares = math.floor(risk / (entry - stop))
            if shares < 1:
                s["status"] = "SKIPPED_SIZING"
                continue
            s.update({
                "status": "OPEN", "entry_open": entry, "stop_price": stop,
                "target_price": entry + TP_ATR_MULT * s["atr14_signal"],
                "shares": shares, "risk_eur": risk, "sessions_held": 1,
            })
            if bar["open"] <= stop:
                close_position(s, day, bar["open"], "GAP_SL")
            elif bar["low"] <= stop:
                close_position(s, day, stop, "SL")
            elif bar["high"] >= s["target_price"]:
                close_position(s, day, s["target_price"], "TP")
            elif TIME_STOP_SESSIONS <= 1:
                close_position(s, day, bar["close"], "T7")
            else:
                open_positions.append(s)
    return signals


def _persist_replay(conn, signals: list[dict]):
    now = datetime.now().isoformat()
    fields = [
        "status", "entry_date", "entry_open", "stop_price", "target_price",
        "shares", "risk_eur", "sessions_held", "exit_date", "exit_price",
        "exit_reason", "pnl_pct_gross", "pnl_pct_net", "pnl_eur_net",
        "r_multiple_net", "capital_after",
    ]
    for s in signals:
        values = [s.get(k) for k in fields]
        conn.execute(
            f"UPDATE sidi_shadow_signals SET {', '.join(f'{k}=?' for k in fields)}, updated_at=? "
            "WHERE ticker=? AND signal_date=? AND strategy_version=?",
            (*values, now, s["ticker"], s["signal_date"], STRATEGY_VERSION),
        )
    conn.commit()


def update_shadow_tracker(all_prices: dict, data_dir: Path | None = None) -> dict:
    conn = _turso_conn()
    if conn is None:
        return {"ok": False, "reason": "turso_not_configured"}
    data_dir = data_dir or Path(__file__).resolve().parents[1] / "data" / "master"
    source = _latest_closed_csv(data_dir)
    _insert_signals(conn, _load_new_signals(source) if source else [])
    signals = _load_signals(conn)
    replayed = _replay(signals, _bars_by_ticker(all_prices))
    _persist_replay(conn, replayed)
    return {
        "ok": True,
        "source": source.name if source else None,
        "signals": len(replayed),
        "open": sum(s["status"] == "OPEN" for s in replayed),
        "closed": sum(s["status"] == "CLOSED" for s in replayed),
    }
