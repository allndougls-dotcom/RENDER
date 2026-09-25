"""
SIDI STOCKS - Servidor de despliegue (local y Render)
=======================================================
Sirve stock-radar-v3.html y expone:
- API de datos existente: status, data, market, hot, trigger
- /api/latest-csv
- /api/registro (GET/POST/DELETE) respaldado en Turso
- API SIDI para ChatGPT Work
- memoria incremental FULL / DELTA / REUSE en Turso

Variables de entorno:
    TURSO_DATABASE_URL
    TURSO_AUTH_TOKEN
    UPDATE_TOKEN
    SIDI_FULL_REFRESH_DAYS       (opcional, default 7)
    SIDI_CACHE_WRITE_TOKEN       (opcional; si se define protege escrituras del cache)

Uso local:
    python servidor_local.py

Uso Render:
    Start Command: python servidor_local.py
"""

import http.server
import socketserver
import json
import os
import sys
import re
import threading
import webbrowser
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, unquote


PORT = int(os.environ.get("PORT", 8000))
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data" / "master"
UPDATE_TOKEN = os.environ.get("UPDATE_TOKEN", "stock-radar-2026")
IS_RENDER = os.environ.get("RENDER", "").lower() == "true" or "RENDER" in os.environ

TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
SIDI_FULL_REFRESH_DAYS = int(os.environ.get("SIDI_FULL_REFRESH_DAYS", "7"))
SIDI_CACHE_WRITE_TOKEN = os.environ.get("SIDI_CACHE_WRITE_TOKEN", "")


def get_latest_csv():
    if not DATA_DIR.exists():
        return None
    csvs = sorted(DATA_DIR.glob("sp500_full_export_*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return csvs[0] if csvs else None


def load_market_context():
    latest = get_latest_csv()
    if not latest:
        return {}, []
    try:
        import csv as csv_module
        with open(latest, newline="", encoding="utf-8") as f:
            rows = list(csv_module.DictReader(f))
        if not rows:
            return {}, []
        first = rows[0]
        market = {
            "regime": first.get("market_regime", "DESCONOCIDO"),
            "spy_price": first.get("spy_price", 0),
            "spy_vs200": first.get("spy_vs200", 0),
            "spy_rsi": first.get("spy_rsi", 50),
            "vix": first.get("vix", None),
        }
        return market, rows
    except Exception as e:
        print(f"  ⚠ Error leyendo CSV: {e}", flush=True)
        return {}, []


def _float(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default=None):
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"true", "1", "yes", "si", "sí"}


def _split_pipe(value):
    if not value:
        return []
    return [x.strip() for x in str(value).split("|") if x.strip()]


def _analysis_date(rows):
    if rows:
        value = (rows[0].get("data_vintage") or "").strip()
        if value:
            return value
    latest = get_latest_csv()
    if latest:
        m = re.search(r"(\d{8})", latest.name)
        if m:
            raw = m.group(1)
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return datetime.now().strftime("%Y-%m-%d")


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _days_between(start, end):
    a = _parse_date(start)
    b = _parse_date(end)
    if not a or not b:
        return None
    return (b - a).days


def _find_row(rows, ticker):
    ticker = (ticker or "").strip().upper()
    return next((r for r in rows if (r.get("ticker") or "").strip().upper() == ticker), None)


def _row_to_sidi_company(row):
    price = _float(row.get("price"))
    atr = _float(row.get("atr_14"))
    spy_vs200 = _float(row.get("spy_vs200"))
    stop_loss = round(price * 0.95, 4) if price is not None else None
    tp1 = round(price + atr, 4) if price is not None and atr is not None else None
    tp2 = round(price + 1.5 * atr, 4) if price is not None and atr is not None else None
    warnings_raw = (row.get("warnings") or "").strip()
    warnings = [] if not warnings_raw or warnings_raw.upper() == "OK" else _split_pipe(warnings_raw)
    roe = _float(row.get("roe"))
    fcf_yield = _float(row.get("fcf_yield_calc"))
    revenue_growth = _float(row.get("revenue_growth"))
    eps_growth = _float(row.get("eps_growth"))
    earnings_days = _int(row.get("earnings_days_next"))

    return {
        "ticker": row.get("ticker"),
        "name": row.get("name"),
        "sector": row.get("sector"),
        "industry": row.get("industry") or None,
        "subsector": row.get("subsector") or None,
        "selection": {
            "setup_hot": _bool(row.get("setup_hot")),
            "full_setup": _bool(row.get("full_setup")),
            "combined_score": _float(row.get("combined_score")),
            "horizon": row.get("horizon") or None,
        },
        "market_context": {
            "regime": row.get("market_regime") or "DESCONOCIDO",
            "spy_above_sma200": (spy_vs200 >= 0) if spy_vs200 is not None else None,
            "spy_vs_sma200_pct": spy_vs200,
            "spy_price": _float(row.get("spy_price")),
            "spy_rsi": _float(row.get("spy_rsi")),
            "vix": _float(row.get("vix")),
        },
        "technical": {
            "price": price,
            "drawdown_60d_pct": abs(_float(row.get("drawdown_60d"), 0.0)),
            "rsi_14": _float(row.get("rsi_14")),
            "macd_improving": _bool(row.get("macd_improving")),
            "golden_cross": _bool(row.get("golden_cross")),
            "near_support": _bool(row.get("near_support")),
            "trend_bias": row.get("trend_bias") or None,
            "technical_score": _float(row.get("tech_score")),
            "sma_50": _float(row.get("sma_50")),
            "sma_200": _float(row.get("sma_200")),
            "price_vs_200ma_pct": _float(row.get("price_vs_200ma_pct")),
            "atr": atr,
        },
        "risk_plan": {
            "stop_loss_pct": -5.0,
            "stop_loss": stop_loss,
            "target_tp1_1x_atr": tp1,
            "target_tp2_1_5x_atr": tp2,
        },
        "fundamentals": {
            "fundamental_score": _float(row.get("fund_score")),
            "growth_score": _float(row.get("fund_growth")),
            "solidity_score": _float(row.get("fund_solidity")),
            "valuation_score": _float(row.get("fund_valuation")),
            "pe": _float(row.get("pe")),
            "forward_pe": _float(row.get("forward_pe")),
            "roe_pct": roe * 100 if roe is not None else None,
            "debt_equity": _float(row.get("debt_equity")),
            "fcf_ni_ratio": _float(row.get("fcf_ni_ratio")),
            "fcf_yield_pct": fcf_yield * 100 if fcf_yield is not None else None,
            "revenue_growth_pct": revenue_growth * 100 if revenue_growth is not None else None,
            "eps_growth_pct": eps_growth * 100 if eps_growth is not None else None,
            "shares_yoy_pct": None,
        },
        "earnings_data": {
            "earnings_days_next": earnings_days,
            "earnings_within_7_days": earnings_days <= 7 if earnings_days is not None else None,
            "next_earnings_date": row.get("earnings_date") or None,
            "latest_earnings_date": row.get("latest_earnings_date") or None,
            "eps_actual": _float(row.get("eps_actual")),
            "eps_estimate": _float(row.get("eps_estimate")),
            "eps_surprise_pct": _float(row.get("eps_surprise_pct")),
            "revenue_actual": None,
            "revenue_estimate": None,
            "revenue_surprise_pct": None,
            "guidance_status": None,
            "margin_trend": None,
        },
        "sector_context": {
            "sector_etf": row.get("sector_etf") or None,
            "peer_group": _split_pipe(row.get("peer_group")),
            "critical_macro_variables": _split_pipe(row.get("critical_macro_variables")),
        },
        "analyst_revisions": {
            "eps_revision_trend": None,
            "revenue_revision_trend": None,
            "price_target_trend": None,
            "rating_trend": None,
            "revision_breadth": None,
        },
        "raw_news": [],
        "technical_alerts": {
            "warnings": warnings,
            "warning_count": _int(row.get("warning_count"), 0),
            "market_filter_rec": row.get("market_filter_rec") or None,
        },
        "data_metadata": {
            "data_vintage": row.get("data_vintage") or None,
            "source": "SIDI master CSV",
        },
    }


# ══════════════════════════════════════════════════════════════════
# HISTÓRICO DE PRECIOS BAJO DEMANDA (ficha de empresa en el frontend)
# ══════════════════════════════════════════════════════════════════
# A diferencia del resto de datos (que vienen precalculados en el CSV
# de la ingesta diaria, cubriendo las 503 empresas a la vez), el
# histórico de precio para el gráfico de la ficha se pide solo para
# UN ticker cuando el usuario realmente abre esa ficha — por eso no
# tiene sentido meterlo en el pipeline batch (inflaría el CSV con
# ~250 días × 503 tickers a diario sin necesidad). Se descarga aquí,
# bajo demanda, vía yfinance, con una caché en memoria de corta
# duración para no repetir la llamada si se reabre la misma ficha.
_PRECIO_HIST_CACHE = {}
_PRECIO_HIST_CACHE_TTL_SEGUNDOS = 15 * 60  # 15 min — suficiente para una sesión de consulta, sin servir datos obsoletos al día siguiente
_PRECIO_HIST_LOCK = threading.Lock()


def get_precio_historico(ticker, dias=180):
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None, "ticker_vacio"

    ahora = datetime.now()
    with _PRECIO_HIST_LOCK:
        cacheado = _PRECIO_HIST_CACHE.get(ticker)
        if cacheado and (ahora - cacheado["fetched_at"]).total_seconds() < _PRECIO_HIST_CACHE_TTL_SEGUNDOS:
            return cacheado["data"], None

    try:
        import yfinance as yf
        import pandas as pd
        end = ahora.strftime("%Y-%m-%d")
        start = (ahora - timedelta(days=int(dias * 1.6))).strftime("%Y-%m-%d")  # margen extra para que MA200 no salga con huecos al inicio del rango visible
        raw = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
        if raw is None or raw.empty:
            return None, "sin_datos_yfinance"

        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw = raw.reset_index()

        closes = raw["Close"].astype(float)
        ma50 = closes.rolling(50).mean()
        ma200 = closes.rolling(200).mean()

        puntos = []
        for i, row in raw.iterrows():
            puntos.append({
                "date": str(row["Date"])[:10],
                "close": round(float(closes.iloc[i]), 2),
                "ma50": round(float(ma50.iloc[i]), 2) if not pd.isna(ma50.iloc[i]) else None,
                "ma200": round(float(ma200.iloc[i]), 2) if not pd.isna(ma200.iloc[i]) else None,
            })

        # Recortar al rango visible pedido (dias) — el margen extra de arriba
        # era solo para que MA50/MA200 llegasen ya formadas desde el primer
        # punto que se muestra, no para enseñarlo todo.
        puntos = puntos[-dias:] if len(puntos) > dias else puntos

        resultado = {"ticker": ticker, "history": puntos}
        with _PRECIO_HIST_LOCK:
            _PRECIO_HIST_CACHE[ticker] = {"data": resultado, "fetched_at": ahora}
        return resultado, None

    except Exception as e:
        print(f"  ⚠ Error descargando histórico de {ticker}: {e}", flush=True)
        return None, str(e)


def _select_sidi_rows(rows, scope="hot", tickers=None):
    scope = (scope or "hot").strip().lower()
    if scope in {"full", "full_setup", "signal", "signals"}:
        selected = [r for r in rows if _bool(r.get("full_setup"))]
        normalized_scope = "full_setup"
    elif scope in {"all", "universe"}:
        selected = list(rows)
        normalized_scope = "all"
    else:
        selected = [r for r in rows if _bool(r.get("setup_hot"))]
        normalized_scope = "setup_hot"
    if tickers:
        wanted = {t.strip().upper() for t in tickers if t.strip()}
        selected = [r for r in selected if (r.get("ticker") or "").strip().upper() in wanted]
    return normalized_scope, selected


REGISTRO_COLUMNS = [
    "fechaDeteccion", "ticker", "name", "sector", "score", "drawdown60",
    "rsi", "pe", "warnings", "warningCount", "estado", "precioEntrada",
    "precioActual", "precioResolucion", "fechaResolucion",
    "diasHastaResolucion", "pnlPct", "diasTranscurridos", "sourceFile",
    "fechaRegistroTimestamp", "editadoManualmente",
]
CACHE_COLUMNS = [
    "ticker", "last_full_analysis_date", "last_check_date", "news_score",
    "verdict", "confidence", "data_quality", "latest_earnings_date_seen",
    "analysis_json", "sources_json", "updated_at",
]
_turso_conn = None
_turso_lock = threading.Lock()


def turso_disponible():
    return bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)


def get_turso_conn():
    global _turso_conn
    if _turso_conn is not None:
        return _turso_conn
    with _turso_lock:
        if _turso_conn is not None:
            return _turso_conn
        import libsql
        conn = libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS registro (
                ticker TEXT NOT NULL,
                fechaDeteccion TEXT NOT NULL,
                name TEXT, sector TEXT,
                score REAL, drawdown60 REAL, rsi REAL, pe REAL,
                warnings TEXT, warningCount INTEGER,
                estado TEXT NOT NULL DEFAULT 'PENDING',
                precioEntrada REAL, precioActual REAL, precioResolucion REAL,
                fechaResolucion TEXT, diasHastaResolucion INTEGER,
                pnlPct REAL, diasTranscurridos INTEGER,
                sourceFile TEXT, fechaRegistroTimestamp TEXT,
                editadoManualmente INTEGER DEFAULT 0,
                PRIMARY KEY (ticker, fechaDeteccion)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sidi_analysis_cache (
                ticker TEXT PRIMARY KEY,
                last_full_analysis_date TEXT,
                last_check_date TEXT,
                news_score REAL,
                verdict TEXT,
                confidence TEXT,
                data_quality TEXT,
                latest_earnings_date_seen TEXT,
                analysis_json TEXT NOT NULL,
                sources_json TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        _turso_conn = conn
        return conn


def registro_row_to_dict(row, cols):
    d = dict(zip(cols, row))
    if d.get("warnings"):
        try:
            d["warnings"] = json.loads(d["warnings"])
        except (json.JSONDecodeError, TypeError):
            d["warnings"] = [d["warnings"]] if d["warnings"] else []
    else:
        d["warnings"] = []
    d["editadoManualmente"] = bool(d.get("editadoManualmente"))
    return d


def registro_get_all():
    conn = get_turso_conn()
    cols = REGISTRO_COLUMNS
    rs = conn.execute(f"SELECT {', '.join(cols)} FROM registro ORDER BY fechaDeteccion DESC")
    rows = rs.fetchall() if hasattr(rs, "fetchall") else list(rs)
    return [registro_row_to_dict(r, cols) for r in rows]


def registro_upsert_many(señales):
    conn = get_turso_conn()
    nuevas, actualizadas = 0, 0
    for s in señales:
        ticker = (s.get("ticker") or "").strip()
        fecha = s.get("fechaDeteccion") or datetime.now().strftime("%Y-%m-%d")
        if not ticker:
            continue
        warnings = s.get("warnings", [])
        warnings_json = json.dumps(warnings if isinstance(warnings, list) else ([warnings] if warnings else []), ensure_ascii=False)
        existe = conn.execute("SELECT 1 FROM registro WHERE ticker = ? AND fechaDeteccion = ?", (ticker, fecha)).fetchone()
        params = (
            s.get("name"), s.get("sector"), s.get("score"), s.get("drawdown60"), s.get("rsi"), s.get("pe"),
            warnings_json, s.get("warningCount"), s.get("estado", "PENDING"), s.get("precioEntrada"),
            s.get("precioActual"), s.get("precioResolucion"), s.get("fechaResolucion"), s.get("diasHastaResolucion"),
            s.get("pnlPct"), s.get("diasTranscurridos"), s.get("sourceFile"),
            s.get("fechaRegistroTimestamp") or datetime.now().isoformat(), 1 if s.get("editadoManualmente") else 0,
            ticker, fecha,
        )
        if existe:
            conn.execute("""
                UPDATE registro SET name=?, sector=?, score=?, drawdown60=?, rsi=?, pe=?, warnings=?, warningCount=?,
                estado=?, precioEntrada=?, precioActual=?, precioResolucion=?, fechaResolucion=?, diasHastaResolucion=?,
                pnlPct=?, diasTranscurridos=?, sourceFile=?, fechaRegistroTimestamp=?, editadoManualmente=?
                WHERE ticker=? AND fechaDeteccion=?
            """, params)
            actualizadas += 1
        else:
            conn.execute("""
                INSERT INTO registro (name, sector, score, drawdown60, rsi, pe, warnings, warningCount, estado,
                precioEntrada, precioActual, precioResolucion, fechaResolucion, diasHastaResolucion, pnlPct,
                diasTranscurridos, sourceFile, fechaRegistroTimestamp, editadoManualmente, ticker, fechaDeteccion)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, params)
            nuevas += 1
    conn.commit()
    return nuevas, actualizadas


def registro_delete_one(ticker, fecha):
    conn = get_turso_conn()
    conn.execute("DELETE FROM registro WHERE ticker = ? AND fechaDeteccion = ?", (ticker, fecha))
    conn.commit()


def registro_delete_all():
    conn = get_turso_conn()
    conn.execute("DELETE FROM registro")
    conn.commit()


def _cache_row_to_dict(row):
    d = dict(zip(CACHE_COLUMNS, row))
    for key in ("analysis_json", "sources_json"):
        raw = d.get(key)
        target = key.replace("_json", "")
        if raw:
            try:
                d[target] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                d[target] = None
        else:
            d[target] = None
    return d


def analysis_cache_get(ticker):
    if not turso_disponible():
        return None
    conn = get_turso_conn()
    rs = conn.execute(f"SELECT {', '.join(CACHE_COLUMNS)} FROM sidi_analysis_cache WHERE ticker = ?", ((ticker or "").strip().upper(),))
    row = rs.fetchone()
    return _cache_row_to_dict(row) if row else None


def analysis_cache_get_all():
    if not turso_disponible():
        return []
    conn = get_turso_conn()
    rs = conn.execute(f"SELECT {', '.join(CACHE_COLUMNS)} FROM sidi_analysis_cache ORDER BY updated_at DESC")
    rows = rs.fetchall() if hasattr(rs, "fetchall") else list(rs)
    return [_cache_row_to_dict(row) for row in rows]


def _analysis_memory_for_row(row, analysis_date):
    ticker = (row.get("ticker") or "").strip().upper()
    if not turso_disponible():
        return {"mode": "FULL", "reason": "cache_unavailable", "previous_analysis": None}
    try:
        cached = analysis_cache_get(ticker)
    except Exception as e:
        print(f"  ⚠ Cache SIDI no disponible para {ticker}: {e}", flush=True)
        return {"mode": "FULL", "reason": "cache_error", "previous_analysis": None}
    if not cached:
        return {"mode": "FULL
