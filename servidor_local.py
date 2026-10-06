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
    GITHUB_ACTIONS_TOKEN         (token fine-grained con Actions: write)
    SIDI_GITHUB_REPOSITORY       (opcional, default allndougls-dotcom/RENDER)
    SIDI_GITHUB_WORKFLOW_FILE    (opcional, default actualizar-datos.yml)
    SIDI_GITHUB_WORKFLOW_REF     (opcional, default main)
    SIDI_FULL_REFRESH_DAYS       (opcional, default 7)
    SIDI_CACHE_WRITE_TOKEN       (opcional; si se define protege escrituras del cache)
    SIDI_CONTROL_CENTER_WRITE_TOKEN (opcional; por defecto usa el token del cache)

Uso local:
    python servidor_local.py

Uso Render:
    Start Command: python servidor_local.py
"""

import http.server
import socketserver
import json
import hashlib
import hmac
import os
import sys
import re
import threading
import webbrowser
from pathlib import Path
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs, quote, unquote
from urllib.request import Request, urlopen

from modules.control_center_v7 import (
    backup_state as control_center_backup_state,
    ensure_schema as ensure_control_center_v7_schema,
    hydrate_analysis as hydrate_v7_analysis,
    upsert_work_position as upsert_v7_work_position,
)

PORT = int(os.environ.get("PORT", 8000))
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data" / "master"
UPDATE_TOKEN = os.environ.get("UPDATE_TOKEN", "")
IS_RENDER = os.environ.get("RENDER", "").lower() == "true" or "RENDER" in os.environ
GITHUB_ACTIONS_TOKEN = os.environ.get("GITHUB_ACTIONS_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("SIDI_GITHUB_REPOSITORY", "allndougls-dotcom/RENDER")
GITHUB_WORKFLOW_FILE = os.environ.get("SIDI_GITHUB_WORKFLOW_FILE", "actualizar-datos.yml")
GITHUB_WORKFLOW_REF = os.environ.get("SIDI_GITHUB_WORKFLOW_REF", "main")
GITHUB_API_VERSION = "2022-11-28"
_github_status_lock = threading.Lock()
_github_dispatch_lock = threading.Lock()
_github_status_cache = {"fetched_at": None, "data": None}

TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
SIDI_FULL_REFRESH_DAYS = int(os.environ.get("SIDI_FULL_REFRESH_DAYS", "7"))
SIDI_CACHE_WRITE_TOKEN = os.environ.get("SIDI_CACHE_WRITE_TOKEN", "")
SIDI_CONTROL_CENTER_WRITE_TOKEN = os.environ.get(
    "SIDI_CONTROL_CENTER_WRITE_TOKEN", SIDI_CACHE_WRITE_TOKEN
)

CONTROL_CENTER_DEFAULT_SETTINGS = {
    "capital": 10_000,
    "riskPct": 1.5,
    "riskMax": 150,
    "maxPositions": 3,
    "portfolioRiskPct": 4.5,
    "warnDays": 4,
    "expireDays": 8,
    "earningsBlockDays": 7,
    "moveReanalysePct": 10,
}


def _github_actions_configured():
    return bool(
        GITHUB_ACTIONS_TOKEN
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", GITHUB_REPOSITORY)
        and GITHUB_WORKFLOW_FILE
        and GITHUB_WORKFLOW_REF
    )


def _github_api_request(method, path, payload=None):
    """Llama a GitHub sin exponer nunca el token al navegador."""
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "SIDI-Control-Center",
    }
    if GITHUB_ACTIONS_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_ACTIONS_TOKEN}"
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        f"https://api.github.com{path}", data=body, headers=headers, method=method
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read()
            data = json.loads(raw.decode("utf-8")) if raw else None
            return response.status, data
    except HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            detail = {"message": raw}
        return error.code, detail
    except URLError as error:
        raise ConnectionError(f"github_unreachable: {error.reason}") from error


def _github_workflow_path(suffix=""):
    owner, repo = GITHUB_REPOSITORY.split("/", 1)
    workflow = quote(GITHUB_WORKFLOW_FILE, safe="")
    return f"/repos/{owner}/{repo}/actions/workflows/{workflow}{suffix}"


def _public_workflow_run(run):
    if not isinstance(run, dict):
        return None
    return {
        "id": run.get("id"),
        "runNumber": run.get("run_number"),
        "event": run.get("event"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "createdAt": run.get("created_at"),
        "updatedAt": run.get("updated_at"),
        "url": run.get("html_url"),
        "headSha": str(run.get("head_sha") or "")[:12],
    }


def github_ingesta_status(force=False):
    """Devuelve el último run del workflow y progreso basado en pasos reales."""
    now = datetime.now(timezone.utc)
    with _github_status_lock:
        fetched_at = _github_status_cache.get("fetched_at")
        if (not force and fetched_at and _github_status_cache.get("data")
                and (now - fetched_at).total_seconds() < 8):
            return _github_status_cache["data"]

    if not _github_actions_configured():
        return {"configured": False, "run": None}
    query = f"?branch={quote(GITHUB_WORKFLOW_REF, safe='')}&per_page=5"
    status, payload = _github_api_request("GET", _github_workflow_path("/runs") + query)
    if status != 200:
        message = payload.get("message") if isinstance(payload, dict) else "GitHub API error"
        raise RuntimeError(f"github_runs_http_{status}: {message}")
    runs = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
    run = runs[0] if runs else None
    public_run = _public_workflow_run(run)
    progress = 0
    current_step = None
    if public_run:
        if public_run["status"] == "queued":
            progress = 5
            current_step = "En cola en GitHub Actions"
        elif public_run["status"] == "in_progress":
            progress = 15
            jobs_status, jobs_payload = _github_api_request(
                "GET",
                f"/repos/{GITHUB_REPOSITORY}/actions/runs/{public_run['id']}/jobs?per_page=100",
            )
            if jobs_status == 200 and isinstance(jobs_payload, dict):
                steps = [
                    step for job in jobs_payload.get("jobs", [])
                    for step in (job.get("steps") or [])
                ]
                relevant = [step for step in steps if not str(step.get("name") or "").startswith("Post ")]
                complete = [step for step in relevant if step.get("status") == "completed"]
                if relevant:
                    progress = max(10, min(95, round(len(complete) / len(relevant) * 100)))
                active = next((step for step in relevant if step.get("status") == "in_progress"), None)
                current_step = active.get("name") if active else "Ejecutando workflow"
        elif public_run["status"] == "completed":
            progress = 100
            current_step = "Completado" if public_run["conclusion"] == "success" else "Finalizado con error"
    result = {
        "configured": True,
        "repository": GITHUB_REPOSITORY,
        "workflow": GITHUB_WORKFLOW_FILE,
        "ref": GITHUB_WORKFLOW_REF,
        "run": public_run,
        "progress": progress,
        "currentStep": current_step,
    }
    with _github_status_lock:
        _github_status_cache.update({"fetched_at": now, "data": result})
    return result


def github_dispatch_ingesta():
    if not _github_actions_configured():
        raise RuntimeError("github_actions_not_configured")
    with _github_dispatch_lock:
        current = github_ingesta_status(force=True)
        run = current.get("run")
        if run and run.get("status") in {"queued", "in_progress"}:
            return {"dispatched": False, "alreadyRunning": True, **current}
        status, payload = _github_api_request(
            "POST", _github_workflow_path("/dispatches"), {"ref": GITHUB_WORKFLOW_REF}
        )
        if status != 204:
            message = payload.get("message") if isinstance(payload, dict) else "GitHub API error"
            raise RuntimeError(f"github_dispatch_http_{status}: {message}")
        with _github_status_lock:
            _github_status_cache.update({"fetched_at": None, "data": None})
        return {
            "configured": True,
            "dispatched": True,
            "alreadyRunning": False,
            "requestedAt": datetime.now(timezone.utc).isoformat(),
            "repository": GITHUB_REPOSITORY,
            "workflow": GITHUB_WORKFLOW_FILE,
            "ref": GITHUB_WORKFLOW_REF,
            "run": None,
        }


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
    strategy_version = row.get("sidi_strategy_version") or "SIDI_INTRADAY_V2"
    stop_loss = round(price * 0.95, 4) if price is not None else None
    tp1_indicative = round(price + atr, 4) if price is not None and atr is not None else None
    tp2_indicative = round(price + 1.5 * atr, 4) if price is not None and atr is not None else None
    warnings_raw = (row.get("warnings") or "").strip()
    warnings = [] if not warnings_raw or warnings_raw.upper() == "OK" else _split_pipe(warnings_raw)
    roe = _float(row.get("roe"))
    fcf_yield = _float(row.get("fcf_yield_calc"))
    revenue_growth = _float(row.get("revenue_growth"))
    eps_growth = _float(row.get("eps_growth"))
    earnings_days = _int(row.get("earnings_days_next"))

    return {
        "ticker": row.get("ticker"),
        "signal_date": row.get("price_date") or row.get("data_vintage") or None,
        "name": row.get("name"),
        "sector": row.get("sector"),
        "industry": row.get("industry") or None,
        "subsector": row.get("subsector") or None,
        "selection": {
            "setup_hot": _bool(row.get("setup_hot")),
            "full_setup": _bool(row.get("full_setup")),
            "strategy_version": strategy_version,
            "gate_failures": row.get("sidi_gate_failures") or None,
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
            "spy_return_20d_pct": _float(row.get("spy_return_20d")),
            "abnormal_return_20d_pct": _float(row.get("abnormal_return_20d")),
            "sidi_context_ready": _bool(row.get("sidi_context_ready")),
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
            "strategy_version": strategy_version,
            "entry_rule": row.get("sidi_entry_rule") or "POST_ANALYSIS_ACTUAL_FILL",
            "entry_status": row.get("sidi_entry_status") or "PENDING_ANALYSIS",
            "entry_reference_source": row.get("sidi_entry_reference_source") or "LIVE_QUOTE_AT_ANALYSIS",
            "actual_fill_required": _bool(row.get("sidi_actual_fill_required"), True),
            "atr14_signal": _float(row.get("sidi_atr14_signal") or row.get("atr_14")),
            "target_tp1_atr_multiple": _float(row.get("sidi_tp1_atr_mult"), 1.0),
            "target_tp2_atr_multiple": _float(row.get("sidi_tp2_atr_mult"), 1.5),
            "stop_loss_pct": _float(row.get("sidi_sl_pct"), -5.0),
            "time_stop_sessions": _int(row.get("sidi_time_stop_sessions"), 7),
            "risk_pct": _float(row.get("sidi_risk_pct"), 1.5),
            "max_positions": _int(row.get("sidi_max_positions"), 3),
            "partial_exit_rule": row.get("sidi_partial_exit_rule") or "SELL_50_PCT_AT_TP1_REST_AT_TP2",
            "after_tp1_stop_rule": row.get("sidi_after_tp1_stop_rule") or "MOVE_REMAINDER_TO_BREAK_EVEN",
            "gap_stop_rule": row.get("sidi_gap_stop_rule") or "EXIT_AT_OPEN_IF_OPEN_BELOW_SL",
            "intraday_conflict_rule": row.get("sidi_intraday_conflict_rule") or "SL_FIRST",
            "stop_loss_indicative_from_signal_close": stop_loss,
            "target_tp1_indicative_from_signal_close": tp1_indicative,
            "target_tp2_indicative_from_signal_close": tp2_indicative,
            "note": (
                "Signal-close levels are indicative. Final SL/TP1/TP2 and sizing "
                "use the actual broker fill after the qualitative verdict."
            ),
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
            "price_date": row.get("price_date") or None,
            "ingestion_timing": "NYSE_OPEN_PLUS_5_MINUTES",
            "analysis_window": "SAME_SESSION_AFTER_INGESTION",
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sidi_control_center_state (
                id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sidi_shadow_signals (
                ticker TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                company TEXT, sector TEXT,
                combined_score REAL, fund_score REAL, dd60 REAL, rsi14 REAL,
                atr14_signal REAL NOT NULL, signal_close REAL,
                status TEXT NOT NULL,
                entry_date TEXT, entry_open REAL, stop_price REAL,
                target_price REAL, shares INTEGER, risk_eur REAL,
                sessions_held INTEGER, exit_date TEXT, exit_price REAL,
                exit_reason TEXT, pnl_pct_gross REAL, pnl_pct_net REAL,
                pnl_eur_net REAL, r_multiple_net REAL, capital_after REAL,
                snapshot_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (ticker, signal_date, strategy_version)
            )
        """)
        ensure_control_center_v7_schema(conn)
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


CONTROL_CENTER_SCHEMA_VERSION = 7
OPERABLE_VERDICTS = {"VALIDADA", "VALIDADA CON CONDICIONES"}
ACTIVE_OPERATION_STATUSES = {"PLANNED", "OPEN", "TP1"}
NEW_YORK = ZoneInfo("America/New_York")


def _observed_holiday(day):
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year, month, weekday, occurrence):
    day = date(year, month, 1)
    return day + timedelta(days=(weekday - day.weekday()) % 7 + 7 * (occurrence - 1))


def _last_weekday(year, month, weekday):
    if month == 12:
        day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        day = date(year, month + 1, 1) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def _easter_sunday(year):
    """Algoritmo gregoriano de Meeus/Jones/Butcher."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    month_seed = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * month_seed) // 451
    month = (h + month_seed - 7 * m + 114) // 31
    day = (h + month_seed - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _nyse_holidays(year):
    holidays = {
        _observed_holiday(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),       # Martin Luther King Jr.
        _nth_weekday(year, 2, 0, 3),       # Presidents Day
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),         # Memorial Day
        _observed_holiday(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),       # Labor Day
        _nth_weekday(year, 11, 3, 4),      # Thanksgiving
        _observed_holiday(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed_holiday(date(year, 6, 19)))
    return holidays


def _next_nyse_session(signal_date):
    day = signal_date + timedelta(days=1)
    while day.weekday() >= 5 or day in _nyse_holidays(day.year):
        day += timedelta(days=1)
    return day


def _analysis_is_on_time(signal_date, analysis_date, completed_at=None):
    """Compatibilidad histórica de SIDI_SHADOW_V1: hasta el Open T+1."""
    try:
        signal = date.fromisoformat(str(signal_date)[:10])
        analysis = date.fromisoformat(str(analysis_date)[:10])
    except (TypeError, ValueError):
        return False
    if analysis <= signal:
        return True
    if analysis != _next_nyse_session(signal):
        return False
    completed_at = completed_at or datetime.now(timezone.utc)
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    completed_ny = completed_at.astimezone(NEW_YORK)
    return completed_ny.date() == analysis and completed_ny.time() < time(9, 30)


def _analysis_route_timing(strategy_version, signal_date, analysis_date, completed_at=None):
    """Devuelve (operable_ahora, estado, motivo) sin fabricar entradas pasadas."""
    strategy = str(strategy_version or "").upper()
    if strategy != "SIDI_INTRADAY_V2":
        on_time = _analysis_is_on_time(signal_date, analysis_date, completed_at)
        return (
            on_time,
            "ON_TIME" if on_time else "LATE_ANALYSIS",
            "AUTO_VERDICT_OPERABLE" if on_time else "ANALYSIS_LATE",
        )

    try:
        signal = date.fromisoformat(str(signal_date)[:10])
        analysis = date.fromisoformat(str(analysis_date)[:10])
    except (TypeError, ValueError):
        return False, "INVALID_SIGNAL_DATE", "INVALID_SIGNAL_DATE"

    expected_session = _next_nyse_session(signal)
    if analysis != expected_session:
        return False, "STALE_SIGNAL", "ANALYSIS_NOT_SIGNAL_SESSION"

    completed_at = completed_at or datetime.now(timezone.utc)
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    completed_ny = completed_at.astimezone(NEW_YORK)
    if completed_ny.date() != analysis:
        return False, "STALE_SIGNAL", "ANALYSIS_NOT_SIGNAL_SESSION"
    if completed_ny.time() < time(9, 35):
        return False, "WAITING_MARKET_DATA", "ANALYSIS_BEFORE_INGESTION"
    if completed_ny.time() >= time(15, 45):
        return False, "MARKET_CLOSED", "ANALYSIS_AFTER_ENTRY_WINDOW"
    return True, "ACTIONABLE_INTRADAY", "AUTO_VERDICT_OPERABLE_INTRADAY"


def _setup_key(strategy_version, ticker, signal_date):
    return "|".join([
        str(strategy_version or "LEGACY").upper(),
        str(ticker or "").upper(),
        str(signal_date or "")[:10],
    ])


def _migrate_control_center_state(state):
    """Migra a V7 sin borrar históricos ni operaciones LIVE."""
    if not isinstance(state, dict):
        state = {}
    previous_schema = _int(state.get("schemaVersion"), 0) or 0
    state["settings"] = {**CONTROL_CENTER_DEFAULT_SETTINGS, **(state.get("settings") or {})}
    if previous_schema < 6:
        state["settings"]["maxPositions"] = 3
        state["settings"]["portfolioRiskPct"] = 4.5
    analyses = state.get("analyses") if isinstance(state.get("analyses"), list) else []
    trades = state.get("trades") if isinstance(state.get("trades"), list) else []

    by_id = {}
    by_identity = {}
    for analysis in analyses:
        if not isinstance(analysis, dict):
            continue
        analysis["ticker"] = str(analysis.get("ticker") or "").upper()
        analysis["strategyVersion"] = analysis.get("strategyVersion") or "LEGACY"
        analysis["signalDate"] = str(analysis.get("signalDate") or analysis.get("date") or "")[:10]
        analysis["analysisDate"] = str(analysis.get("analysisDate") or analysis.get("date") or analysis["signalDate"])[:10]
        analysis["date"] = analysis["analysisDate"]
        if not (analysis.get("episodeId") and analysis.get("setupKey")):
            analysis["setupKey"] = _setup_key(
                analysis["strategyVersion"], analysis["ticker"],
                analysis.get("episodeStartDate") or analysis["signalDate"],
            )
        analysis["id"] = analysis.get("id") or "S-" + hashlib.sha256(analysis["setupKey"].encode()).hexdigest()[:16]
        analysis.setdefault("route", "REGISTRY")
        analysis.setdefault("routingReason", "LEGACY_MIGRATION" if analysis["strategyVersion"] == "LEGACY" else "VERDICT_ARCHIVE")
        by_id[analysis["id"]] = analysis
        by_identity[analysis["setupKey"]] = analysis

    duplicate_fields = {
        "ticker", "analysisNewsScore", "analysisVerdict", "analysisSource",
        "strategyVersion", "sector", "fallReason", "signalDate", "referencePrice",
    }
    for operation in trades:
        if not isinstance(operation, dict):
            continue
        setup = by_id.get(operation.get("setupId") or operation.get("analysisId"))
        if not setup:
            identity = _setup_key(
                operation.get("strategyVersion"), operation.get("ticker"),
                operation.get("signalDate") or operation.get("entryDate"),
            )
            setup = by_identity.get(identity)
        if setup:
            operation["setupId"] = setup["id"]
            operation.pop("analysisId", None)
            for field in duplicate_fields:
                operation.pop(field, None)
            if operation.get("status") in ACTIVE_OPERATION_STATUSES:
                setup["route"] = "OPERATIONS"
                setup["routingReason"] = "ACTIVE_OPERATION"
            elif operation.get("status") == "CLOSED":
                setup["route"] = "REGISTRY"
                setup["routingReason"] = "LIVE_CLOSED"

    state["analyses"] = analyses
    state["trades"] = trades
    state["schemaVersion"] = CONTROL_CENTER_SCHEMA_VERSION
    return state


def _hydrate_shadow_results(state, conn):
    """Hidrata V7 por episodio y mantiene V1 únicamente como histórico."""
    try:
        columns = [
            "ticker", "signal_date", "strategy_version", "status", "entry_date",
            "entry_open", "stop_price", "target_price", "shares", "risk_eur",
            "sessions_held", "exit_date", "exit_price", "exit_reason",
            "pnl_pct_net", "pnl_eur_net", "r_multiple_net", "capital_after",
        ]
        rows = conn.execute(
            f"SELECT {', '.join(columns)} FROM sidi_shadow_signals"
        ).fetchall()
        shadow_by_key = {
            _setup_key(row[2], row[0], row[1]): dict(zip(columns, row))
            for row in rows
        }
        for analysis in state.get("analyses", []):
            analysis.pop("shadow", None)
            if analysis.get("episodeId"):
                hydrate_v7_analysis(conn, analysis)
            else:
                shadow = shadow_by_key.get(analysis.get("setupKey"))
                if shadow:
                    analysis["shadow"] = shadow
    except Exception:
        pass
    return state


def control_center_get():
    """Devuelve el maestro V7: episodios analizados + operaciones enlazadas."""
    conn = get_turso_conn()
    row = conn.execute(
        "SELECT schema_version, state_json, updated_at "
        "FROM sidi_control_center_state WHERE id = ?",
        ("main",),
    ).fetchone()
    if not row:
        return None
    _, raw, updated_at = row
    state = _hydrate_shadow_results(_migrate_control_center_state(json.loads(raw)), conn)
    return {
        "schema_version": CONTROL_CENTER_SCHEMA_VERSION,
        "updated_at": updated_at,
        "state": state,
    }


def control_center_save(state, schema_version=CONTROL_CENTER_SCHEMA_VERSION):
    """Guarda el maestro; Shadow se rehidrata desde su tabla y no se duplica."""
    state = _migrate_control_center_state(state)
    if not isinstance(state.get("analyses"), list):
        raise ValueError("state.analyses debe ser una lista")
    if not isinstance(state.get("trades"), list):
        raise ValueError("state.trades debe ser una lista")
    state_to_save = json.loads(json.dumps(state, ensure_ascii=False))
    for analysis in state_to_save["analyses"]:
        analysis.pop("shadow", None)
        analysis.pop("revisions", None)
    now = datetime.now().isoformat()
    raw = json.dumps(state_to_save, ensure_ascii=False, separators=(",", ":"))
    conn = get_turso_conn()
    conn.execute("""
        INSERT INTO sidi_control_center_state
            (id, schema_version, state_json, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            schema_version = excluded.schema_version,
            state_json = excluded.state_json,
            updated_at = excluded.updated_at
    """, ("main", CONTROL_CENTER_SCHEMA_VERSION, raw, now))
    conn.commit()
    return now


def control_center_cleanup_v7():
    """Limpia la vista activa sin destruir V1/LEGACY; todo queda respaldado."""
    saved = control_center_get()
    if not saved or not isinstance(saved.get("state"), dict):
        return {"backup_id": None, "archived_analyses": 0, "archived_trades": 0}
    conn = get_turso_conn()
    state = saved["state"]
    backup_id = control_center_backup_state(
        conn, state, saved.get("schema_version", 0), "CONTROL_CENTER_V7_CLEANUP"
    )
    archived_analyses = list(state.get("archivedAnalyses") or [])
    keep_analyses, moved_ids = [], set()
    for analysis in state.get("analyses", []):
        if str(analysis.get("strategyVersion") or "").upper() == "SIDI_INTRADAY_V2":
            keep_analyses.append(analysis)
        else:
            archived = dict(analysis)
            archived["archived"] = True
            archived["archiveReason"] = "PRE_V7_HISTORY"
            archived.pop("shadow", None)
            archived.pop("revisions", None)
            archived_analyses.append(archived)
            moved_ids.add(analysis.get("id"))

    archived_trades = list(state.get("archivedTrades") or [])
    moved_trades = 0
    keep_trades = []
    for trade in state.get("trades", []):
        if trade.get("setupId") in moved_ids or not trade.get("setupId"):
            archived = dict(trade)
            if archived.get("status") in ACTIVE_OPERATION_STATUSES:
                archived["status"] = "CANCELLED"
                archived["cancelReason"] = "PRE_V7_HISTORY"
                archived["cancelledAt"] = datetime.now(timezone.utc).isoformat()
            archived_trades.append(archived)
            moved_trades += 1
        else:
            keep_trades.append(trade)

    state["analyses"] = keep_analyses
    state["trades"] = keep_trades
    state["archivedAnalyses"] = archived_analyses
    state["archivedTrades"] = archived_trades
    state["schemaVersion"] = CONTROL_CENTER_SCHEMA_VERSION
    state["cleanupBackupId"] = backup_id
    conn.execute(
        "UPDATE sidi_shadow_signals SET status='INVALID_MIGRATED_V7' "
        "WHERE status='WAITING_ENTRY'"
    )
    conn.commit()
    control_center_save(state, CONTROL_CENTER_SCHEMA_VERSION)
    return {
        "backup_id": backup_id,
        "archived_analyses": len(moved_ids),
        "archived_trades": moved_trades,
        "active_analyses": len(keep_analyses),
        "active_trades": len(keep_trades),
    }


def _first_value(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _string_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return _split_pipe(value)


def _control_center_analysis(position, analysis_date):
    """Normaliza el JSON de ChatGPT Work al contrato interno del Control Center."""
    trade = position.get("trade_plan") if isinstance(position.get("trade_plan"), dict) else {}
    risk = position.get("risk_plan") if isinstance(position.get("risk_plan"), dict) else {}
    technical = position.get("technical") if isinstance(position.get("technical"), dict) else {}
    selection = position.get("selection") if isinstance(position.get("selection"), dict) else {}
    ticker = str(position.get("ticker") or "").strip().upper()
    if not ticker:
        return None

    date_value = str(_first_value(position.get("analysis_date"), analysis_date) or "")[:10]
    signal_date = str(_first_value(
        position.get("signal_date"), technical.get("price_date"),
        position.get("price_date"),
    ) or "")[:10]
    price_eur = _float(_first_value(position.get("price_current_eur"), position.get("price_eur")), 0.0)
    price_usd = _float(_first_value(position.get("price_current_usd"), position.get("price_usd"), technical.get("price")), 0.0)
    atr_usd = _float(_first_value(
        position.get("atr14_signal"), position.get("atr14_usd"),
        position.get("atr14_signal_usd"), trade.get("atr14_signal"),
        trade.get("atr14_signal_usd"), risk.get("atr14_signal"), technical.get("atr"),
    ), 0.0)
    atr_eur = _float(_first_value(
        position.get("atr14_signal_eur"), position.get("atr14_eur"),
        trade.get("atr14_signal_eur"), risk.get("atr14_signal_eur"),
    ), 0.0)
    if not atr_eur and atr_usd and price_eur and price_usd:
        atr_eur = atr_usd * price_eur / price_usd

    strategy_version = str(_first_value(
        position.get("strategy_version"), trade.get("strategy_version"),
        risk.get("strategy_version"), selection.get("strategy_version"),
        "SIDI_INTRADAY_V2",
    ))
    is_intraday_v2 = strategy_version.upper() == "SIDI_INTRADAY_V2"
    full_setup = _bool(_first_value(
        position.get("full_setup"), position.get("sidi_full_setup"), selection.get("full_setup"),
    ))
    entry_reference_eur = _float(_first_value(
        trade.get("entry_reference_eur"), position.get("entry_reference_eur"),
        trade.get("entry_ideal_eur"), position.get("entry_ideal_eur"),
        price_eur if is_intraday_v2 else None,
    ), 0.0)
    entry_reference_usd = _float(_first_value(
        trade.get("entry_reference_usd"), position.get("entry_reference_usd"),
        price_usd if is_intraday_v2 else None,
    ), 0.0)

    normalized = {
        "date": date_value or datetime.now().strftime("%Y-%m-%d"),
        "analysisDate": date_value or datetime.now().strftime("%Y-%m-%d"),
        "signalDate": signal_date,
        "ticker": ticker,
        "company": str(_first_value(position.get("company"), position.get("name")) or ""),
        "sector": str(position.get("sector") or ""),
        "subsector": str(position.get("subsector") or ""),
        "priceEur": price_eur,
        "priceUsd": price_usd,
        "signalCloseUsd": _float(_first_value(position.get("signal_close_usd"), technical.get("price")), 0.0),
        "marketOpenUsd": _float(_first_value(position.get("market_open_usd"), position.get("day_open")), 0.0),
        "entryReferenceEur": entry_reference_eur,
        "entryReferenceUsd": entry_reference_usd,
        "actualFillEur": _float(_first_value(trade.get("actual_fill_eur"), position.get("actual_fill_eur")), 0.0),
        "actualFillUsd": _float(_first_value(trade.get("actual_fill_usd"), position.get("actual_fill_usd")), 0.0),
        "dd60": _float(_first_value(position.get("dd60d_pct"), position.get("dd60"), position.get("drawdown_60d_pct")), 0.0),
        "rsi": _float(_first_value(position.get("rsi_14"), position.get("rsi")), 0.0),
        "newsScore": _float(_first_value(position.get("news_score"), position.get("score")), 0.0),
        "verdict": str(position.get("verdict") or "").upper(),
        "confidence": str(position.get("confidence") or ""),
        "dataQuality": str(position.get("data_quality") or ""),
        "decision": str(position.get("decision") or ""),
        "strategyVersion": strategy_version,
        "fullSetup": full_setup,
        "atr14Usd": atr_usd,
        "atr14Eur": atr_eur,
        "entryRule": str(_first_value(
            position.get("entry_rule"), trade.get("entry_rule"), risk.get("entry_rule"),
            "POST_ANALYSIS_ACTUAL_FILL" if is_intraday_v2 else
            ("NEXT_SESSION_OPEN" if strategy_version.upper() == "SIDI_SHADOW_V1" else ""),
        )),
        "entry": entry_reference_eur,
        "entryAlt": _float(_first_value(trade.get("entry_alternative_eur"), position.get("entry_alternative_eur")), 0.0),
        "sl": _float(_first_value(trade.get("stop_loss_eur"), position.get("stop_loss_eur")), 0.0),
        "tp1": _float(_first_value(trade.get("tp_eur"), trade.get("tp1_eur"), position.get("tp_eur"), position.get("tp1_eur")), 0.0),
        "tp2": _float(_first_value(trade.get("tp2_eur"), position.get("tp2_eur")), 0.0),
        "shares": _int(_first_value(trade.get("shares"), position.get("shares")), 0),
        "risk": _float(_first_value(trade.get("max_risk_eur"), risk.get("max_risk_eur"), position.get("max_risk_eur")), 0.0),
        "notional": _float(_first_value(trade.get("notional_eur"), position.get("notional_eur")), 0.0),
        "earningsDate": str(_first_value(position.get("earnings_date_next"), position.get("earnings_date")) or "")[:10],
        "fallReason": str(position.get("fall_reason") or ""),
        "blockers": _string_list(position.get("blockers")),
        "penalties": _string_list(position.get("penalties")),
        "catalysts": _string_list(position.get("catalysts")),
        "strengths": _string_list(position.get("strengths") or position.get("catalysts")),
        "weaknesses": _string_list(position.get("weaknesses") or position.get("penalties")),
        "newsItems": position.get("news_items") if isinstance(position.get("news_items"), list) else [],
        "thesis": str(_first_value(position.get("thesis_short"), position.get("thesis")) or ""),
        "analysisSource": "CHATGPT_WORK",
    }
    fingerprint_payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    normalized["sourceFingerprint"] = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
    return normalized


def control_center_upsert_analysis_payload(payload):
    """Registra solo FULL analizadas y las enruta sin duplicar datos."""
    sidi = payload.get("sidi_excel_payload") if isinstance(payload, dict) else None
    if not sidi and isinstance(payload, dict):
        sidi = payload
    if not isinstance(sidi, dict):
        raise ValueError("Falta sidi_excel_payload")
    positions = sidi.get("positions") or []
    if not isinstance(positions, list):
        raise ValueError("positions debe ser una lista")
    analysis_date = str(sidi.get("analysis_date") or datetime.now().strftime("%Y-%m-%d"))[:10]
    research_sources = payload.get("research_sources", {}) if isinstance(payload, dict) else {}

    saved = control_center_get()
    state = saved.get("state") if saved else None
    if not isinstance(state, dict):
        state = {"settings": dict(CONTROL_CENTER_DEFAULT_SETTINGS), "analyses": [], "trades": []}
    state = _migrate_control_center_state(state)

    inserted, updated, unchanged, ignored = [], [], [], []
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    for position in positions:
        if not isinstance(position, dict):
            continue
        analysis = _control_center_analysis(position, analysis_date)
        if not analysis:
            continue
        if (analysis["strategyVersion"].upper() in {"SIDI_SHADOW_V1", "SIDI_INTRADAY_V2"}
                and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", analysis["signalDate"])):
            raise ValueError(
                f"signal_date obligatorio para {analysis['ticker']} en "
                f"{analysis['strategyVersion']}"
            )
        if not analysis["fullSetup"]:
            ignored.append(analysis["ticker"])
            continue
        actionable, timing_status, timing_reason = _analysis_route_timing(
            analysis["strategyVersion"], analysis["signalDate"],
            analysis["analysisDate"], now_dt,
        )
        daily_setup_key = _setup_key(
            analysis["strategyVersion"], analysis["ticker"], analysis["signalDate"]
        )
        v7_meta = None
        if analysis["strategyVersion"].upper() == "SIDI_INTRADAY_V2":
            sources = research_sources.get(analysis["ticker"], []) if isinstance(research_sources, dict) else []
            v7_meta = upsert_v7_work_position(
                get_turso_conn(), analysis, position, analysis_date, sources,
                completed_at=now_dt, actionable=actionable,
                timing_reason=timing_reason,
            )
            analysis["episodeId"] = v7_meta["episode_id"]
            analysis["analysisKey"] = v7_meta["analysis_key"]
            existing = next((item for item in state["analyses"]
                             if item.get("episodeId") == analysis["episodeId"]), None)
            analysis["setupKey"] = existing.get("setupKey") if existing else daily_setup_key
            analysis["episodeStartDate"] = (
                existing.get("episodeStartDate") or existing.get("signalDate")
                if existing else analysis["signalDate"]
            )
        else:
            analysis["setupKey"] = daily_setup_key
            existing = next((item for item in state["analyses"]
                             if item.get("setupKey") == analysis["setupKey"]), None)
        same_payload = bool(existing and existing.get("sourceFingerprint") == analysis["sourceFingerprint"])
        if same_payload:
            unchanged.append(analysis["ticker"])
            continue
        elif existing:
            analysis["id"] = existing.get("id")
            analysis["createdAt"] = existing.get("createdAt") or now
            analysis["verdictOriginal"] = existing.get("verdictOriginal") or existing.get("verdict")
            analysis["newsScoreOriginal"] = existing.get("newsScoreOriginal", existing.get("newsScore"))
            analysis["analysisCompletedAt"] = existing.get("analysisCompletedAt") or existing.get("updatedAt") or now
            analysis["revision"] = int(existing.get("revision") or 1) + 1
            analysis["updatedAt"] = now
            existing.clear()
            existing.update(analysis)
            updated.append(analysis["ticker"])
        elif not existing:
            analysis.update({
                "id": "S-" + hashlib.sha256(analysis["setupKey"].encode()).hexdigest()[:16],
                "createdAt": now,
                "updatedAt": now,
                "analysisCompletedAt": now,
                "verdictOriginal": analysis["verdict"],
                "newsScoreOriginal": analysis["newsScore"],
                "revision": 1,
            })
            state["analyses"].append(analysis)
            inserted.append(analysis["ticker"])

        current = existing if existing else analysis
        setup_operations = [trade_item for trade_item in state["trades"]
                            if trade_item.get("setupId") == current["id"]]
        active = next((trade_item for trade_item in setup_operations
                       if trade_item.get("status") in ACTIVE_OPERATION_STATUSES), None)
        closed = next((trade_item for trade_item in setup_operations
                       if trade_item.get("status") == "CLOSED"), None)
        cancelled = next((trade_item for trade_item in setup_operations
                          if trade_item.get("status") == "CANCELLED"), None)
        operable = current["verdict"] in OPERABLE_VERDICTS
        if closed:
            current["route"] = "REGISTRY"
            current["routingReason"] = "LIVE_CLOSED"
            current["timingStatus"] = timing_status
        elif active and active.get("status") in {"OPEN", "TP1"}:
            current["route"] = "OPERATIONS"
            current["routingReason"] = "ACTIVE_OPERATION"
            current["timingStatus"] = timing_status
        elif operable and actionable and current.get("atr14Eur"):
            current["route"] = "OPERATIONS"
            current["routingReason"] = timing_reason
            current["timingStatus"] = timing_status
            if not active and cancelled:
                cancelled["status"] = "PLANNED"
                cancelled["createdAt"] = now
                cancelled.pop("cancelReason", None)
                cancelled.pop("cancelledAt", None)
            elif not active:
                state["trades"].append({
                    "id": "O-" + hashlib.sha256(current["setupKey"].encode()).hexdigest()[:16],
                    "setupId": current["id"],
                    "status": "PLANNED",
                    "createdAt": now,
                })
        else:
            current["route"] = "REGISTRY"
            current["timingStatus"] = timing_status
            if not operable:
                current["routingReason"] = "VERDICT_" + current["verdict"].replace(" ", "_")
            elif not actionable:
                current["routingReason"] = timing_reason
            else:
                current["routingReason"] = "MISSING_ATR"
            if active and active.get("status") == "PLANNED":
                active["status"] = "CANCELLED"
                active["cancelReason"] = current["routingReason"]
                active["cancelledAt"] = now

    if inserted or updated or unchanged or not saved:
        state["schemaVersion"] = CONTROL_CENTER_SCHEMA_VERSION
        state["updatedAt"] = now
        control_center_save(state, CONTROL_CENTER_SCHEMA_VERSION)
    return {
        "analysis_date": analysis_date,
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "ignored_non_full": ignored,
        "count": len(inserted) + len(updated),
    }


def shadow_get_all():
    conn = get_turso_conn()
    columns = [
        "ticker", "signal_date", "strategy_version", "company", "sector",
        "combined_score", "fund_score", "dd60", "rsi14", "atr14_signal",
        "signal_close", "status", "entry_date", "entry_open", "stop_price",
        "target_price", "shares", "risk_eur", "sessions_held", "exit_date",
        "exit_price", "exit_reason", "pnl_pct_gross", "pnl_pct_net",
        "pnl_eur_net", "r_multiple_net", "capital_after", "created_at", "updated_at",
    ]
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM sidi_shadow_signals "
        "ORDER BY signal_date DESC, combined_score DESC, ticker"
    ).fetchall()
    return [dict(zip(columns, row)) for row in rows]


def shadow_v2_get_all():
    conn = get_turso_conn()
    ensure_control_center_v7_schema(conn)
    columns = [
        "episode_id", "ticker", "strategy_version", "status",
        "original_verdict", "latest_verdict", "analysis_completed_at",
        "entry_date", "entry_ts", "entry_price_usd", "entry_price_eur",
        "atr14_usd", "stop_price_usd", "tp1_price_usd", "tp2_price_usd",
        "shares", "risk_eur", "sessions_held", "tp1_hit", "tp1_date",
        "exit_date", "exit_price_usd", "exit_reason", "pnl_pct_net",
        "pnl_eur_net", "r_multiple_net", "mfe_pct", "mae_pct",
        "last_mark_date", "invalid_reason", "created_at", "updated_at",
    ]
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM sidi_shadow_trades_v2 "
        "ORDER BY created_at DESC, ticker"
    ).fetchall()
    return [dict(zip(columns, row)) for row in rows]


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


# ══════════════════════════════════════════════════════════════
# HISTÓRICO DE PRECIOS BAJO DEMANDA (gráfico de la ficha de empresa)
# ══════════════════════════════════════════════════════════════
# El CSV maestro no guarda series de precios, así que el gráfico tipo
# Yahoo/Google Finance de la ficha se pide aquí al abrirla. Se cachea en
# memoria por (ticker, rango) para no repetir descargas en la misma sesión.
HISTORY_RANGES = {
    # rango: (period yfinance, interval, TTL cache en segundos)
    "1d":  ("1d",  "5m",  120),
    "5d":  ("5d",  "30m", 300),
    "1mo": ("1mo", "1d",  900),
    "6mo": ("6mo", "1d",  900),
    "ytd": ("ytd", "1d",  900),
    "1y":  ("1y",  "1d",  900),
    "5y":  ("5y",  "1wk", 3600),
    "max": ("max", "1mo", 3600),
}
_HISTORY_CACHE = {}
_HISTORY_LOCK = threading.Lock()

# Medias móviles del gráfico. Necesitan histórico previo al rango visible
# (200 sesiones ≈ 290 días naturales), así que en estos rangos se descarga
# desde "inicio del rango - margen" y luego se recorta. En 5A las velas son
# semanales: 10 y 40 semanas equivalen a las medias de 50 y 200 sesiones.
HISTORY_MA_DIAS_VISIBLES = {"1mo": 31, "6mo": 183, "1y": 366, "5y": 5 * 365 + 2}
HISTORY_MA_MARGEN_DIAS = 310


def get_price_history(ticker, rango="6mo"):
    ticker = (ticker or "").strip().upper()
    if not re.match(r"^[A-Z0-9.\-^=]{1,15}$", ticker):
        raise ValueError("ticker_invalido")
    if rango not in HISTORY_RANGES:
        raise ValueError("rango_invalido")
    period, interval, ttl = HISTORY_RANGES[rango]
    key = (ticker, rango)
    ahora = datetime.now()
    with _HISTORY_LOCK:
        hit = _HISTORY_CACHE.get(key)
        if hit and (ahora - hit["fetched_at"]).total_seconds() < ttl:
            return hit["data"]

    import yfinance as yf
    import pandas as pd
    yf_ticker = yf.Ticker(ticker.replace(".", "-"))  # Yahoo usa BRK-B, no BRK.B

    con_ma = rango in HISTORY_MA_DIAS_VISIBLES or rango == "ytd"
    if con_ma:
        inicio = (datetime(ahora.year, 1, 1) if rango == "ytd"
                  else ahora - pd.Timedelta(days=HISTORY_MA_DIAS_VISIBLES[rango]))
        df = yf_ticker.history(start=(inicio - pd.Timedelta(days=HISTORY_MA_MARGEN_DIAS)).strftime("%Y-%m-%d"),
                               interval=interval, auto_adjust=True)
    else:
        df = yf_ticker.history(period=period, interval=interval, auto_adjust=True)
    if df is None or df.empty:
        raise LookupError("sin_datos")
    df = df.dropna(subset=["Close"])

    ma = None
    if con_ma:
        rapida, lenta = (10, 40) if interval == "1wk" else (50, 200)
        df = df.assign(ma_fast=df["Close"].rolling(rapida).mean(), ma_slow=df["Close"].rolling(lenta).mean())
        corte = pd.Timestamp(inicio)
        corte = corte.tz_localize(df.index.tz) if df.index.tz is not None else corte
        df = df[df.index >= corte]
        if df.empty:
            raise LookupError("sin_datos")
        ma = {"fast": 50, "slow": 200, "bars": [rapida, lenta], "interval": interval}

    def _num(v):
        return round(float(v), 4) if v is not None and v == v else None

    intraday = interval.endswith("m")
    puntos = []
    for idx, row in df.iterrows():
        vol = row.get("Volume")
        extra = {"ma50": _num(row.get("ma_fast")), "ma200": _num(row.get("ma_slow"))} if con_ma else {}
        puntos.append({**extra,
            "t": idx.strftime("%Y-%m-%dT%H:%M") if intraday else idx.strftime("%Y-%m-%d"),
            "o": round(float(row["Open"]), 4),
            "h": round(float(row["High"]), 4),
            "l": round(float(row["Low"]), 4),
            "c": round(float(row["Close"]), 4),
            "v": int(vol) if vol is not None and vol == vol else 0,  # vol == vol descarta NaN
        })

    # En 1D la variación se mide contra el cierre anterior, como Yahoo/Google
    prev_close = None
    if rango == "1d":
        try:
            daily = yf_ticker.history(period="5d", interval="1d", auto_adjust=True).dropna(subset=["Close"])
            ultimo_dia = df.index[-1].date()
            previos = daily[[d.date() < ultimo_dia for d in daily.index]]
            if not previos.empty:
                prev_close = round(float(previos["Close"].iloc[-1]), 4)
        except Exception:
            prev_close = None

    data = {"ticker": ticker, "range": rango, "interval": interval, "intraday": intraday,
            "prev_close": prev_close, "ma": ma, "points": puntos, "fetched_at": ahora.isoformat()}
    with _HISTORY_LOCK:
        _HISTORY_CACHE[key] = {"data": data, "fetched_at": ahora}
    return data


PWA_FILES = {
    "/sw.js": "application/javascript; charset=utf-8",
    "/manifest.json": "application/manifest+json; charset=utf-8",
}


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
        elif path in PWA_FILES: self.serve_pwa_file(path)
        elif path == "/api/latest-csv": self.handle_latest_csv()
        elif path == "/api/registro": self.handle_registro_get()
        elif path == "/api/sidi/control-center": self.handle_control_center_get()
        elif path == "/api/ingesta/status": self.handle_ingesta_status()
        elif path == "/api/sidi/shadow": self.handle_shadow_get()
        elif path == "/api/sidi/shadow-v2": self.handle_shadow_v2_get()
        elif path == "/api/sidi/status": self.handle_sidi_status()
        elif path == "/api/sidi/market": self.handle_sidi_market()
        elif path == "/api/sidi/candidates": self.handle_sidi_candidates(query)
        elif path == "/api/sidi/work-packet": self.handle_sidi_work_packet(query)
        elif path == "/api/sidi/analysis-cache": self.handle_analysis_cache_get()
        elif path == "/api/sidi/analysis-cache/save": self.handle_analysis_cache_save_page()
        else:
            m = re.match(r"^/api/sidi/candidates/([^/]+)$", path)
            c = re.match(r"^/api/sidi/analysis-cache/([^/]+)$", path)
            h = re.match(r"^/api/history/([^/]+)$", path)
            if m: self.handle_sidi_candidate(unquote(m.group(1)))
            elif h: self.handle_history(unquote(h.group(1)), query)
            elif c: self.handle_analysis_cache_get(unquote(c.group(1)))
            else: super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/trigger": self.handle_trigger()
        elif path == "/api/registro": self.handle_registro_post()
        elif path == "/api/sidi/control-center": self.handle_control_center_post()
        elif path == "/api/sidi/control-center/cleanup-v7": self.handle_control_center_cleanup_v7()
        elif path in {"/api/sidi/analysis-cache", "/api/sidi/work-result"}: self.handle_analysis_cache_post()
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

    def serve_pwa_file(self, path):
        # Service Worker y manifest de la PWA: desde la raíz (el alcance del
        # Service Worker cubre así toda la app), con su Content-Type exacto y
        # sin caché HTTP para que el navegador detecte enseguida los cambios.
        # Los iconos (/icons/*.png) los sirve el manejador estático normal.
        file_path = BASE_DIR / path.lstrip("/")
        if not file_path.exists():
            self.send_error(404); return
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", PWA_FILES[path])
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_root(self):
        _, rows = load_market_context()
        self.send_json({
            "service": "STOCK-RADAR Cloud API", "status": "ok", "version": "1.4", "empresas": len(rows),
            "registro_backend": "turso" if turso_disponible() else "no configurado (usa localStorage)",
            "sidi_cache_backend": "turso" if turso_disponible() else "no configurado", "updated": datetime.now().isoformat(),
            "endpoints": ["/status","/data","/market","/hot","/trigger","/api/ingesta/status","/mobile","/api/latest-csv",
            "/api/registro (GET/POST/DELETE)","/api/sidi/status","/api/sidi/market","/api/sidi/candidates",
            "/api/sidi/candidates/{ticker}","/api/sidi/work-packet","/api/sidi/analysis-cache (GET/POST)",
            "/api/sidi/shadow","/api/sidi/shadow-v2","/api/sidi/control-center (GET/POST)","/api/sidi/work-result (POST)",
            "/api/sidi/control-center/cleanup-v7 (POST)",
            "/api/sidi/analysis-cache/{ticker}","/api/sidi/analysis-cache/save","/api/history/{ticker}?range=1d|5d|1mo|6mo|ytd|1y|5y|max"],
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
            control_center = control_center_upsert_analysis_payload(payload)
            self.send_json({
                "ok": True,
                "analysis_date": analysis_date,
                "saved": saved,
                "count": len(saved),
                "control_center": control_center,
            })
        except json.JSONDecodeError: self.send_json({"ok": False, "error": "JSON inválido"}, status=400)
        except ValueError as e: self.send_json({"ok": False, "error": str(e)}, status=400)
        except Exception as e:
            print(f"  ❌ Error guardando SIDI cache: {e}", flush=True); self.send_json({"ok": False, "error": str(e)}, status=500)

    def handle_analysis_cache_save_page(self):
        html = """<!doctype html><html lang='es'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>SIDI Analysis Cache</title><style>body{font-family:system-ui;max-width:900px;margin:30px auto;padding:0 16px}textarea{width:100%;height:55vh;font-family:monospace}input{width:100%;padding:8px;margin:8px 0}button{padding:10px 18px}pre{white-space:pre-wrap}</style></head><body><h1>Guardar análisis SIDI</h1><p>Pega el JSON final completo de Work.</p><label>Token (solo si SIDI_CACHE_WRITE_TOKEN está configurado)</label><input id='token' type='password'><textarea id='payload' placeholder='{"sidi_excel_payload": {...}}'></textarea><br><button onclick='save()'>Guardar en Turso</button><pre id='result'></pre><script>async function save(){const payload=document.getElementById('payload').value;const token=document.getElementById('token').value;const headers={'Content-Type':'application/json'};if(token)headers['X-SIDI-Cache-Token']=token;try{const r=await fetch('/api/sidi/analysis-cache',{method:'POST',headers,body:payload});document.getElementById('result').textContent=JSON.stringify(await r.json(),null,2)}catch(e){document.getElementById('result').textContent=String(e)}}</script></body></html>"""
        body = html.encode("utf-8"); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def _update_authorized(self):
        token = self.headers.get("X-Update-Token", "")
        return bool(UPDATE_TOKEN) and hmac.compare_digest(token, UPDATE_TOKEN)

    def handle_ingesta_status(self):
        if not UPDATE_TOKEN:
            self.send_json({"ok": False, "error": "update_token_not_configured"}, status=503)
            return
        if not self._update_authorized():
            self.send_json({"ok": False, "error": "invalid_update_token"}, status=401)
            return
        try:
            self.send_json({"ok": True, **github_ingesta_status()})
        except Exception as error:
            print(f"  ❌ Error consultando GitHub Actions: {error}", flush=True)
            self.send_json({"ok": False, "error": str(error)}, status=502)

    def handle_trigger(self):
        if not UPDATE_TOKEN:
            self.send_json({"ok": False, "error": "update_token_not_configured"}, status=503)
            return
        if not self._update_authorized():
            self.send_json({"ok": False, "error": "invalid_update_token"}, status=401)
            return
        if not _github_actions_configured():
            self.send_json({"ok": False, "error": "github_actions_not_configured"}, status=503)
            return
        try:
            result = github_dispatch_ingesta()
            code = 200 if result.get("alreadyRunning") else 202
            self.send_json({"ok": True, **result}, status=code)
        except Exception as error:
            print(f"  ❌ Error lanzando GitHub Actions: {error}", flush=True)
            self.send_json({"ok": False, "error": str(error)}, status=502)

    def handle_history(self, ticker, query):
        rango = (query.get("range", ["6mo"])[0] or "6mo").lower()
        try:
            self.send_json(get_price_history(ticker, rango))
        except ValueError as e:
            self.send_json({"error": str(e), "ranges": list(HISTORY_RANGES)}, status=400)
        except LookupError as e:
            self.send_json({"error": str(e), "ticker": ticker}, status=404)
        except Exception as e:
            print(f"  ⚠ Error descargando histórico de {ticker}: {e}", flush=True)
            self.send_json({"error": "descarga_fallida", "detail": str(e)}, status=502)

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

    def _control_center_write_authorized(self):
        if not SIDI_CONTROL_CENTER_WRITE_TOKEN:
            return True
        return self.headers.get("X-SIDI-Control-Token", "") == SIDI_CONTROL_CENTER_WRITE_TOKEN

    def handle_control_center_get(self):
        if not turso_disponible():
            self.send_json({"ok": False, "error": "turso_not_configured"}, status=503)
            return
        try:
            saved = control_center_get()
            if not saved:
                self.send_json({"ok": True, "exists": False, "state": None})
                return
            self.send_json({"ok": True, "exists": True, **saved})
        except Exception as e:
            print(f"  Error leyendo Control Center: {e}", flush=True)
            self.send_json({"ok": False, "error": str(e)}, status=500)

    def handle_shadow_get(self):
        if not turso_disponible():
            self.send_json({"ok": False, "error": "turso_not_configured"}, status=503)
            return
        try:
            signals = shadow_get_all()
            self.send_json({
                "ok": True,
                "strategy_version": "SIDI_SHADOW_V1",
                "count": len(signals),
                "signals": signals,
            })
        except Exception as e:
            print(f"  Error leyendo Shadow: {e}", flush=True)
            self.send_json({"ok": False, "error": str(e)}, status=500)

    def handle_shadow_v2_get(self):
        if not turso_disponible():
            self.send_json({"ok": False, "error": "turso_not_configured"}, status=503)
            return
        try:
            signals = shadow_v2_get_all()
            self.send_json({
                "ok": True,
                "strategy_version": "SIDI_INTRADAY_V2",
                "count": len(signals),
                "signals": signals,
            })
        except Exception as e:
            print(f"  Error leyendo Shadow V2: {e}", flush=True)
            self.send_json({"ok": False, "error": str(e)}, status=500)

    def handle_control_center_post(self):
        if not turso_disponible():
            self.send_json({"ok": False, "error": "turso_not_configured"}, status=503)
            return
        if not self._control_center_write_authorized():
            self.send_json({"ok": False, "error": "invalid_control_center_token"}, status=401)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8"))
            state = payload.get("state", payload)
            updated_at = control_center_save(state, payload.get("schema_version", CONTROL_CENTER_SCHEMA_VERSION))
            self.send_json({"ok": True, "schema_version": CONTROL_CENTER_SCHEMA_VERSION, "updated_at": updated_at})
        except json.JSONDecodeError:
            self.send_json({"ok": False, "error": "JSON inválido"}, status=400)
        except ValueError as e:
            self.send_json({"ok": False, "error": str(e)}, status=400)
        except Exception as e:
            print(f"  Error guardando Control Center: {e}", flush=True)
            self.send_json({"ok": False, "error": str(e)}, status=500)

    def handle_control_center_cleanup_v7(self):
        if not turso_disponible():
            self.send_json({"ok": False, "error": "turso_not_configured"}, status=503)
            return
        if not self._control_center_write_authorized():
            self.send_json({"ok": False, "error": "invalid_control_center_token"}, status=401)
            return
        try:
            result = control_center_cleanup_v7()
            self.send_json({"ok": True, "schema_version": CONTROL_CENTER_SCHEMA_VERSION, **result})
        except Exception as e:
            print(f"  Error limpiando Control Center V7: {e}", flush=True)
            self.send_json({"ok": False, "error": str(e)}, status=500)

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
