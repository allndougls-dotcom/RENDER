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
from datetime import datetime
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
        return {"mode": "FULL", "reason": "no_previous_analysis", "previous_analysis": None}
    previous_full = cached.get("last_full_analysis_date")
    previous_check = cached.get("last_check_date")
    current_earnings = row.get("latest_earnings_date") or None
    previous_earnings = cached.get("latest_earnings_date_seen")
    if previous_check == analysis_date:
        return {
            "mode": "REUSE", "reason": "already_checked_today", "previous_analysis_date": previous_check,
            "last_full_analysis_date": previous_full, "delta_since": previous_check,
            "previous_news_score": cached.get("news_score"), "previous_verdict": cached.get("verdict"),
            "previous_analysis": cached.get("analysis"), "previous_sources": cached.get("sources"),
        }
    if current_earnings and previous_earnings and str(current_earnings)[:10] != str(previous_earnings)[:10]:
        return {
            "mode": "FULL", "reason": "new_earnings_detected", "previous_analysis_date": previous_check,
            "last_full_analysis_date": previous_full, "previous_analysis": cached.get("analysis"),
        }
    age_days = _days_between(previous_full, analysis_date)
    if age_days is None or age_days >= SIDI_FULL_REFRESH_DAYS:
        return {
            "mode": "FULL", "reason": "stale_full_analysis", "age_days": age_days,
            "refresh_after_days": SIDI_FULL_REFRESH_DAYS, "previous_analysis_date": previous_check,
            "last_full_analysis_date": previous_full, "previous_analysis": cached.get("analysis"),
        }
    return {
        "mode": "DELTA", "reason": "recent_analysis", "previous_analysis_date": previous_check,
        "last_full_analysis_date": previous_full, "delta_since": previous_check or previous_full, "age_days": age_days,
        "previous_news_score": cached.get("news_score"), "previous_verdict": cached.get("verdict"),
        "previous_analysis": cached.get("analysis"), "previous_sources": cached.get("sources"),
    }


def _build_work_packet(rows, scope="hot", tickers=None):
    normalized_scope, selected = _select_sidi_rows(rows, scope, tickers)
    analysis_date = _analysis_date(rows)
    companies = []
    mode_counts = {"FULL": 0, "DELTA": 0, "REUSE": 0}
    for row in selected:
        company = _row_to_sidi_company(row)
        memory = _analysis_memory_for_row(row, analysis_date)
        company["analysis_memory"] = memory
        mode = memory.get("mode", "FULL")
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        companies.append(company)
    return {
        "schema_version": "SIDI_WORK_PACKET_V2", "analysis_date": analysis_date,
        "selection_scope": normalized_scope, "candidate_count": len(selected),
        "analysis_mode_counts": mode_counts, "full_refresh_days": SIDI_FULL_REFRESH_DAYS,
        "operational_parameters": {
            "base_capital_eur": 10000, "max_risk_per_trade_pct": 1.5, "max_risk_per_trade_eur": 150,
            "max_simultaneous_positions": 3, "tp1_sell_pct": 50, "tp2_sell_pct": 50,
            "after_tp1": "move_remaining_stop_to_break_even",
        },
        "companies": companies,
    }


def analysis_cache_upsert_position(position, analysis_date, sources, current_row=None):
    ticker = (position.get("ticker") or "").strip().upper()
    if not ticker:
        return False
    conn = get_turso_conn()
    existing = analysis_cache_get(ticker)
    update_meta = position.get("analysis_update") or {}
    requested_mode = (update_meta.get("mode") or position.get("analysis_mode") or "").strip().upper()
    material_change = update_meta.get("material_change")
    if not requested_mode and current_row is not None:
        requested_mode = _analysis_memory_for_row(current_row, analysis_date).get("mode", "FULL")
    if requested_mode in {"FULL", "FULL_REFRESH"} or not existing:
        last_full = analysis_date
    else:
        last_full = existing.get("last_full_analysis_date")
    latest_earnings = current_row.get("latest_earnings_date") if current_row else None
    if not latest_earnings and existing:
        latest_earnings = existing.get("latest_earnings_date_seen")
    if requested_mode == "DELTA" and material_change is True:
        last_full = analysis_date
    params = (
        ticker, last_full, analysis_date, _float(position.get("news_score")), position.get("verdict"),
        position.get("confidence"), position.get("data_quality"), latest_earnings,
        json.dumps(position, ensure_ascii=False), json.dumps(sources or [], ensure_ascii=False), datetime.now().isoformat(),
    )
    conn.execute("""
        INSERT INTO sidi_analysis_cache (ticker,last_full_analysis_date,last_check_date,news_score,verdict,confidence,
        data_quality,latest_earnings_date_seen,analysis_json,sources_json,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker) DO UPDATE SET last_full_analysis_date=excluded.last_full_analysis_date,
        last_check_date=excluded.last_check_date,news_score=excluded.news_score,verdict=excluded.verdict,
        confidence=excluded.confidence,data_quality=excluded.data_quality,
        latest_earnings_date_seen=excluded.latest_earnings_date_seen,analysis_json=excluded.analysis_json,
        sources_json=excluded.sources_json,updated_at=excluded.updated_at
    """, params)
    conn.commit()
    return True


def analysis_cache_save_payload(payload, rows):
    sidi = payload.get("sidi_excel_payload") if isinstance(payload, dict) else None
    if not sidi and isinstance(payload, dict):
        sidi = payload
    if not isinstance(sidi, dict):
        raise ValueError("Falta sidi_excel_payload")
    positions = sidi.get("positions") or []
    if not isinstance(positions, list):
        raise ValueError("positions debe ser una lista")
    analysis_date = sidi.get("analysis_date") or _analysis_date(rows) or datetime.now().strftime("%Y-%m-%d")
    research_sources = payload.get("research_sources", {}) if isinstance(payload, dict) else {}
    saved = []
    for position in positions:
        if not isinstance(position, dict):
            continue
        ticker = (position.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        row = _find_row(rows, ticker)
        sources = research_sources.get(ticker, []) if isinstance(research_sources, dict) else []
        if analysis_cache_upsert_position(position, analysis_date, sources, current_row=row):
            saved.append(ticker)
    return analysis_date, saved


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path in {"/", ""}:
            self.handle_root()
        elif path == "/status": self.handle_status()
        elif path == "/data": self.handle_data()
        elif path == "/market": self.handle_market()
        elif path == "/hot": self.handle_hot()
        elif path in {"/mobile", "/stock-radar-v3.html"}: self.serve_app()
        elif path == "/api/latest-csv": self.handle_latest_csv()
        elif path == "/api/registro": self.handle_registro_get()
        elif path == "/api/sidi/status": self.handle_sidi_status()
        elif path == "/api/sidi/market": self.handle_sidi_market()
        elif path == "/api/sidi/candidates": self.handle_sidi_candidates(query)
        elif path == "/api/sidi/work-packet": self.handle_sidi_work_packet(query)
        elif path == "/api/sidi/analysis-cache": self.handle_analysis_cache_get()
        elif path == "/api/sidi/analysis-cache/save": self.handle_analysis_cache_save_page()
        else:
            m = re.match(r"^/api/sidi/candidates/([^/]+)$", path)
            c = re.match(r"^/api/sidi/analysis-cache/([^/]+)$", path)
            if m: self.handle_sidi_candidate(unquote(m.group(1)))
            elif c: self.handle_analysis_cache_get(unquote(c.group(1)))
            else: super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/trigger": self.handle_trigger()
        elif path == "/api/registro": self.handle_registro_post()
        elif path == "/api/sidi/analysis-cache": self.handle_analysis_cache_post()
        else: self.send_error(404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        m = re.match(r"^/api/registro/([^/]+)/([^/]+)$", path)
        if path == "/api/registro": self.handle_registro_delete_all()
        elif m: self.handle_registro_delete_one(m.group(1), m.group(2))
        else: self.send_error(404)

    def serve_app(self):
        app_path = BASE_DIR / "stock-radar-v3.html"
        if not app_path.exists():
            self.send_json({"error": "stock-radar-v3.html no encontrado"}, status=404); return
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
        with open(app_path, "rb") as f: self.wfile.write(f.read())

    def handle_root(self):
        _, rows = load_market_context()
        self.send_json({
            "service": "STOCK-RADAR Cloud API", "status": "ok", "version": "1.3", "empresas": len(rows),
            "registro_backend": "turso" if turso_disponible() else "no configurado (usa localStorage)",
            "sidi_cache_backend": "turso" if turso_disponible() else "no configurado", "updated": datetime.now().isoformat(),
            "endpoints": ["/status","/data","/market","/hot","/trigger","/mobile","/api/latest-csv",
            "/api/registro (GET/POST/DELETE)","/api/sidi/status","/api/sidi/market","/api/sidi/candidates",
            "/api/sidi/candidates/{ticker}","/api/sidi/work-packet","/api/sidi/analysis-cache (GET/POST)",
            "/api/sidi/analysis-cache/{ticker}","/api/sidi/analysis-cache/save"],
        })

    def handle_status(self):
        latest = get_latest_csv(); market, rows = load_market_context()
        self.send_json({"ok": True,"empresas": len(rows),"csv_files": len(list(DATA_DIR.glob("sp500_full_export_*.csv"))) if DATA_DIR.exists() else 0,
                        "latest_csv": latest.name if latest else None,"market": market.get("regime", "N/D"),"vix": market.get("vix", 0),"updated": datetime.now().isoformat()})

    def handle_data(self):
        _, rows = load_market_context(); self.send_json({"rows": rows, "count": len(rows)})

    def handle_market(self):
        market, _ = load_market_context(); self.send_json(market)

    def handle_hot(self):
        _, rows = load_market_context(); hot = [r for r in rows if str(r.get("setup_hot", "")).strip() == "True"]
        self.send_json({"hot": hot, "count": len(hot)})

    def handle_latest_csv(self):
        latest = get_latest_csv()
        if not latest: self.send_json({"error": "no_csv_found"}, status=404); return
        mtime = datetime.fromtimestamp(latest.stat().st_mtime)
        self.send_json({"filename": latest.name,"path": f"/data/master/{latest.name}","modified": mtime.isoformat(),
                        "modified_str": mtime.strftime("%d/%m/%Y %H:%M"),"size_kb": round(latest.stat().st_size / 1024, 1)})

    def handle_sidi_status(self):
        latest = get_latest_csv(); market, rows = load_market_context(); _, hot_rows = _select_sidi_rows(rows, "hot"); _, full_rows = _select_sidi_rows(rows, "full")
        cache_count = 0
        if turso_disponible():
            try: cache_count = len(analysis_cache_get_all())
            except Exception: cache_count = 0
        self.send_json({"ok": True,"schema_version": "SIDI_WORK_PACKET_V2","analysis_date": _analysis_date(rows),
                        "latest_csv": latest.name if latest else None,"universe_count": len(rows),"setup_hot_count": len(hot_rows),
                        "full_setup_count": len(full_rows),"analysis_cache_count": cache_count,"full_refresh_days": SIDI_FULL_REFRESH_DAYS,
                        "market_regime": market.get("regime", "N/D"),"updated": datetime.now().isoformat()})

    def handle_sidi_market(self):
        market, rows = load_market_context(); self.send_json({"analysis_date": _analysis_date(rows), "market_context": market})

    def handle_sidi_candidates(self, query):
        _, rows = load_market_context(); scope = (query.get("scope", ["hot"])[0] or "hot").strip(); normalized_scope, selected = _select_sidi_rows(rows, scope); analysis_date = _analysis_date(rows)
        candidates = []
        for row in selected:
            memory = _analysis_memory_for_row(row, analysis_date)
            candidates.append({"ticker": row.get("ticker"),"name": row.get("name"),"sector": row.get("sector"),"industry": row.get("industry") or None,
                               "fundamental_score": _float(row.get("fund_score")),"technical_score": _float(row.get("tech_score")),"combined_score": _float(row.get("combined_score")),
                               "drawdown_60d_pct": abs(_float(row.get("drawdown_60d"), 0.0)),"rsi_14": _float(row.get("rsi_14")),"setup_hot": _bool(row.get("setup_hot")),
                               "full_setup": _bool(row.get("full_setup")),"earnings_days_next": _int(row.get("earnings_days_next")),"analysis_mode": memory.get("mode"),"analysis_reason": memory.get("reason")})
        self.send_json({"analysis_date": analysis_date,"selection_scope": normalized_scope,"count": len(candidates),"candidates": candidates})

    def handle_sidi_candidate(self, ticker):
        _, rows = load_market_context(); ticker = (ticker or "").strip().upper(); row = _find_row(rows, ticker)
        if not row: self.send_json({"error": "ticker_not_found", "ticker": ticker}, status=404); return
        company = _row_to_sidi_company(row); company["analysis_memory"] = _analysis_memory_for_row(row, _analysis_date(rows))
        self.send_json({"schema_version": "SIDI_WORK_PACKET_V2","analysis_date": _analysis_date(rows),"company": company})

    def handle_sidi_work_packet(self, query):
        _, rows = load_market_context(); scope = (query.get("scope", ["hot"])[0] or "hot").strip(); tickers_raw = query.get("tickers", [""])[0]
        tickers = [t for t in tickers_raw.split(",") if t.strip()] if tickers_raw else None
        self.send_json(_build_work_packet(rows, scope=scope, tickers=tickers))

    def handle_analysis_cache_get(self, ticker=None):
        if not turso_disponible(): self.send_json({"ok": False, "error": "turso_not_configured"}, status=503); return
        try:
            if ticker:
                item = analysis_cache_get(ticker)
                if not item: self.send_json({"ok": False, "error": "analysis_not_found"}, status=404); return
                self.send_json({"ok": True, "analysis": item}); return
            items = analysis_cache_get_all(); summaries = []
            for item in items:
                summaries.append({k: item.get(k) for k in ["ticker","last_full_analysis_date","last_check_date","news_score","verdict","confidence","data_quality","latest_earnings_date_seen","updated_at"]})
            self.send_json({"ok": True, "count": len(summaries), "cache": summaries})
        except Exception as e:
            print(f"  ❌ Error leyendo SIDI cache: {e}", flush=True); self.send_json({"ok": False, "error": str(e)}, status=500)

    def _cache_write_authorized(self):
        if not SIDI_CACHE_WRITE_TOKEN: return True
        return self.headers.get("X-SIDI-Cache-Token", "") == SIDI_CACHE_WRITE_TOKEN

    def handle_analysis_cache_post(self):
        if not turso_disponible(): self.send_json({"ok": False, "error": "turso_not_configured"}, status=503); return
        if not self._cache_write_authorized(): self.send_json({"ok": False, "error": "invalid_cache_token"}, status=401); return
        try:
            length = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(length) if length else b"{}"; payload = json.loads(body.decode("utf-8"))
            _, rows = load_market_context(); analysis_date, saved = analysis_cache_save_payload(payload, rows)
            self.send_json({"ok": True, "analysis_date": analysis_date, "saved": saved, "count": len(saved)})
        except json.JSONDecodeError: self.send_json({"ok": False, "error": "JSON inválido"}, status=400)
        except ValueError as e: self.send_json({"ok": False, "error": str(e)}, status=400)
        except Exception as e:
            print(f"  ❌ Error guardando SIDI cache: {e}", flush=True); self.send_json({"ok": False, "error": str(e)}, status=500)

    def handle_analysis_cache_save_page(self):
        html = """<!doctype html><html lang='es'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>SIDI Analysis Cache</title><style>body{font-family:system-ui;max-width:900px;margin:30px auto;padding:0 16px}textarea{width:100%;height:55vh;font-family:monospace}input{width:100%;padding:8px;margin:8px 0}button{padding:10px 18px}pre{white-space:pre-wrap}</style></head><body><h1>Guardar análisis SIDI</h1><p>Pega el JSON final completo de Work.</p><label>Token (solo si SIDI_CACHE_WRITE_TOKEN está configurado)</label><input id='token' type='password'><textarea id='payload' placeholder='{"sidi_excel_payload": {...}}'></textarea><br><button onclick='save()'>Guardar en Turso</button><pre id='result'></pre><script>async function save(){const payload=document.getElementById('payload').value;const token=document.getElementById('token').value;const headers={'Content-Type':'application/json'};if(token)headers['X-SIDI-Cache-Token']=token;try{const r=await fetch('/api/sidi/analysis-cache',{method:'POST',headers,body:payload});document.getElementById('result').textContent=JSON.stringify(await r.json(),null,2)}catch(e){document.getElementById('result').textContent=String(e)}}</script></body></html>"""
        body = html.encode("utf-8"); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def handle_trigger(self):
        token = self.headers.get("X-Update-Token", "")
        if token != UPDATE_TOKEN: self.send_json({"ok": False, "error": "token invalido"}, status=401); return
        def run_ingesta():
            inicio = datetime.now(); print(f"  🚀 Lanzando main_ingesta.py en background ({inicio.strftime('%H:%M:%S')})...", flush=True)
            try:
                resultado = subprocess.run([sys.executable, str(BASE_DIR / "main_ingesta.py")], cwd=str(BASE_DIR), timeout=3600); duracion = (datetime.now() - inicio).total_seconds()
                if resultado.returncode == 0: print(f"  ✅ Ingesta completada correctamente en {duracion:.0f}s", flush=True)
                else:
                    probable = "posible OOM-kill / límite de memoria" if resultado.returncode < 0 else "ver traceback arriba"
                    print(f"  ❌ Ingesta terminó con returncode={resultado.returncode} tras {duracion:.0f}s ({probable})", flush=True)
            except subprocess.TimeoutExpired:
                duracion = (datetime.now() - inicio).total_seconds(); print(f"  ❌ Ingesta cancelada por timeout tras {duracion:.0f}s (límite: 3600s)", flush=True)
            except Exception as e:
                duracion = (datetime.now() - inicio).total_seconds(); print(f"  ❌ Error en ingesta background tras {duracion:.0f}s: {e}", flush=True)
        threading.Thread(target=run_ingesta, daemon=True).start(); self.send_json({"ok": True, "message": "Actualización iniciada en background"})

    def handle_registro_get(self):
        if not turso_disponible(): self.send_json({"ok": False,"error": "turso_not_configured","message": "TURSO_DATABASE_URL / TURSO_AUTH_TOKEN no configuradas en el servidor."}, status=503); return
        try:
            señales = registro_get_all(); self.send_json({"ok": True, "registro": señales, "count": len(señales)})
        except Exception as e: print(f"  ❌ Error leyendo registro de Turso: {e}", flush=True); self.send_json({"ok": False, "error": str(e)}, status=500)

    def handle_registro_post(self):
        if not turso_disponible(): self.send_json({"ok": False,"error": "turso_not_configured","message": "TURSO_DATABASE_URL / TURSO_AUTH_TOKEN no configuradas en el servidor."}, status=503); return
        try:
            length = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(length) if length else b"{}"; payload = json.loads(body.decode("utf-8"))
            señales = payload.get("señales") or payload.get("registro") or payload
            if isinstance(señales, dict): señales = [señales]
            if not isinstance(señales, list): self.send_json({"ok": False, "error": "Se esperaba una lista de señales"}, status=400); return
            nuevas, actualizadas = registro_upsert_many(señales); self.send_json({"ok": True, "nuevas": nuevas, "actualizadas": actualizadas})
        except json.JSONDecodeError: self.send_json({"ok": False, "error": "JSON inválido"}, status=400)
        except Exception as e: print(f"  ❌ Error escribiendo registro en Turso: {e}", flush=True); self.send_json({"ok": False, "error": str(e)}, status=500)

    def handle_registro_delete_one(self, ticker, fecha):
        if not turso_disponible(): self.send_json({"ok": False, "error": "turso_not_configured"}, status=503); return
        try: registro_delete_one(ticker, fecha); self.send_json({"ok": True})
        except Exception as e: self.send_json({"ok": False, "error": str(e)}, status=500)

    def handle_registro_delete_all(self):
        if not turso_disponible(): self.send_json({"ok": False, "error": "turso_not_configured"}, status=503); return
        try: registro_delete_all(); self.send_json({"ok": True})
        except Exception as e: self.send_json({"ok": False, "error": str(e)}, status=500)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8"); self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, format, *args):
        super().log_message(format, *args)


def main():
    os.chdir(BASE_DIR)
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = True
    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        url = f"http://localhost:{PORT}/stock-radar-v3.html"
        print(f"""
  ================================================
   SIDI STOCKS - Servidor activo (puerto {PORT})
  ================================================
   App:      {url}
   Datos:    {DATA_DIR}
   Modo:     {'Render' if IS_RENDER else 'Local'}
   Registro: {'Turso conectado' if turso_disponible() else '⚠ Turso NO configurado'}
   Cache:    {'Turso conectado' if turso_disponible() else '⚠ Turso NO configurado'}
  ================================================
""")
        if not IS_RENDER: threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        try: httpd.serve_forever()
        except KeyboardInterrupt: print("\n  Servidor detenido.")


if __name__ == "__main__":
    main()
