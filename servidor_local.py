"""
SIDI STOCKS - Servidor de despliegue (local y Render)
=======================================================
Sirve stock-radar-v3.html y expone la API de datos que ya usaba el
sistema (status, data, market, hot, trigger) + el endpoint auxiliar
/api/latest-csv que la app usa para auto-cargar el CSV mas reciente
cuando corre con este servidor (en vez de doble clic al HTML suelto).

Ademas expone /api/registro (GET/POST/DELETE) respaldado en Turso
(SQLite en la nube), para que el Registro de señales sobreviva a
redeploys de Render, cambios de version del HTML, o limpiezas de
localStorage del navegador — la base de datos vive fuera del ciclo
de vida del servidor.

Tambien expone una API SIDI de SOLO LECTURA para ChatGPT Work:
    /api/sidi/status
    /api/sidi/market
    /api/sidi/candidates
    /api/sidi/candidates/{ticker}
    /api/sidi/work-packet

Variables de entorno necesarias para el Registro en Turso:
    TURSO_DATABASE_URL  = libsql://tu-base.tu-org.turso.io
    TURSO_AUTH_TOKEN    = el token generado en el dashboard de Turso
Si no están configuradas, /api/registro responde 503 y el HTML cae
automáticamente a usar solo localStorage (comportamiento anterior).

Uso local:
    python servidor_local.py
    -> abre http://localhost:8000/stock-radar-v3.html

Uso en Render:
    Start Command: python servidor_local.py
    Render inyecta el puerto real en la variable de entorno PORT.
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


def get_latest_csv():
    if not DATA_DIR.exists():
        return None
    csvs = sorted(DATA_DIR.glob("sp500_full_export_*.csv"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return csvs[0] if csvs else None


def load_market_context():
    """Lee la primera fila del CSV mas reciente para status/market/hot."""
    latest = get_latest_csv()
    if not latest:
        return {}, []
    try:
        import csv as csv_module
        with open(latest, newline="", encoding="utf-8") as f:
            reader = csv_module.DictReader(f)
            rows = list(reader)
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
        print(f"  ⚠ Error leyendo CSV: {e}")
        return {}, []


# ══════════════════════════════════════════════════════════════
# SIDI WORK API — helpers de solo lectura
# ══════════════════════════════════════════════════════════════

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
        date = (rows[0].get("data_vintage") or "").strip()
        if date:
            return date
    latest = get_latest_csv()
    if latest:
        m = re.search(r"(\d{8})", latest.name)
        if m:
            raw = m.group(1)
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return datetime.now().strftime("%Y-%m-%d")


def _row_to_sidi_company(row):
    """Transforma una fila del CSV maestro al paquete cuantitativo que
    ChatGPT Work necesita. No investiga ni inventa datos externos: los
    campos que requieren web/IR/consenso permanecen en null."""
    price = _float(row.get("price"))
    atr = _float(row.get("atr_14"))
    spy_vs200 = _float(row.get("spy_vs200"))

    stop_loss = round(price * 0.95, 4) if price is not None else None
    tp1 = round(price + atr, 4) if price is not None and atr is not None else None
    tp2 = round(price + 1.5 * atr, 4) if price is not None and atr is not None else None

    warnings_raw = (row.get("warnings") or "").strip()
    warnings = [] if not warnings_raw or warnings_raw.upper() == "OK" else _split_pipe(warnings_raw)

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
            "roe_pct": (_float(row.get("roe")) * 100) if _float(row.get("roe")) is not None else None,
            "debt_equity": _float(row.get("debt_equity")),
            "fcf_ni_ratio": _float(row.get("fcf_ni_ratio")),
            "fcf_yield_pct": (_float(row.get("fcf_yield_calc")) * 100) if _float(row.get("fcf_yield_calc")) is not None else None,
            "revenue_growth_pct": (_float(row.get("revenue_growth")) * 100) if _float(row.get("revenue_growth")) is not None else None,
            "eps_growth_pct": (_float(row.get("eps_growth")) * 100) if _float(row.get("eps_growth")) is not None else None,
            "shares_yoy_pct": None,
        },
        "earnings_data": {
            "earnings_days_next": _int(row.get("earnings_days_next")),
            "earnings_within_7_days": (_int(row.get("earnings_days_next")) <= 7) if _int(row.get("earnings_days_next")) is not None else None,
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
        selected = [r for r in selected if (r.get("ticker") or "").upper() in wanted]

    return normalized_scope, selected


def _build_work_packet(rows, scope="hot", tickers=None):
    normalized_scope, selected = _select_sidi_rows(rows, scope, tickers)
    return {
        "schema_version": "SIDI_WORK_PACKET_V1",
        "analysis_date": _analysis_date(rows),
        "selection_scope": normalized_scope,
        "candidate_count": len(selected),
        "operational_parameters": {
            "base_capital_eur": 10000,
            "max_risk_per_trade_pct": 1.5,
            "max_risk_per_trade_eur": 150,
            "max_simultaneous_positions": 3,
            "tp1_sell_pct": 50,
            "tp2_sell_pct": 50,
            "after_tp1": "move_remaining_stop_to_break_even",
        },
        "companies": [_row_to_sidi_company(r) for r in selected],
    }


# ══════════════════════════════════════════════════════════════
# REGISTRO — persistencia en Turso (SQLite en la nube)
# ══════════════════════════════════════════════════════════════

REGISTRO_COLUMNS = [
    "fechaDeteccion", "ticker", "name", "sector", "score", "drawdown60",
    "rsi", "pe", "warnings", "warningCount", "estado", "precioEntrada",
    "precioActual", "precioResolucion", "fechaResolucion",
    "diasHastaResolucion", "pnlPct", "diasTranscurridos", "sourceFile",
    "fechaRegistroTimestamp", "editadoManualmente",
]

_turso_conn = None
_turso_lock = threading.Lock()


def turso_disponible():
    return bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)


def get_turso_conn():
    """Conexión perezosa y reutilizada a Turso. Thread-safe con un lock
    simple porque http.server con ThreadingMixIn puede atender varias
    peticiones a la vez."""
    global _turso_conn
    if _turso_conn is not None:
        return _turso_conn
    with _turso_lock:
        if _turso_conn is not None:
            return _turso_conn
        import libsql
        conn = libsql.connect(
            database=TURSO_DATABASE_URL,
            auth_token=TURSO_AUTH_TOKEN,
        )
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


def registro_upsert_many(señales: list):
    """Inserta o actualiza cada señal por (ticker, fechaDeteccion).
    Nunca borra nada que no venga explícitamente en la lista — cada
    señal se toca de forma individual, así que un fallo a mitad de
    lote no afecta a las que ya se procesaron."""
    conn = get_turso_conn()
    nuevas, actualizadas = 0, 0

    for s in señales:
        ticker = (s.get("ticker") or "").strip()
        fecha = s.get("fechaDeteccion") or datetime.now().strftime("%Y-%m-%d")
        if not ticker:
            continue

        warnings = s.get("warnings", [])
        if isinstance(warnings, list):
            warnings_json = json.dumps(warnings, ensure_ascii=False)
        else:
            warnings_json = json.dumps([warnings] if warnings else [])

        existe = conn.execute(
            "SELECT 1 FROM registro WHERE ticker = ? AND fechaDeteccion = ?",
            (ticker, fecha)
        ).fetchone()

        params = (
            s.get("name"), s.get("sector"), s.get("score"), s.get("drawdown60"),
            s.get("rsi"), s.get("pe"), warnings_json, s.get("warningCount"),
            s.get("estado", "PENDING"), s.get("precioEntrada"), s.get("precioActual"),
            s.get("precioResolucion"), s.get("fechaResolucion"),
            s.get("diasHastaResolucion"), s.get("pnlPct"), s.get("diasTranscurridos"),
            s.get("sourceFile"),
            s.get("fechaRegistroTimestamp") or datetime.now().isoformat(),
            1 if s.get("editadoManualmente") else 0,
            ticker, fecha,
        )

        if existe:
            conn.execute(f"""
                UPDATE registro SET
                    name=?, sector=?, score=?, drawdown60=?, rsi=?, pe=?,
                    warnings=?, warningCount=?, estado=?, precioEntrada=?,
                    precioActual=?, precioResolucion=?, fechaResolucion=?,
                    diasHastaResolucion=?, pnlPct=?, diasTranscurridos=?,
                    sourceFile=?, fechaRegistroTimestamp=?, editadoManualmente=?
                WHERE ticker=? AND fechaDeteccion=?
            """, params)
            actualizadas += 1
        else:
            conn.execute("""
                INSERT INTO registro (
                    name, sector, score, drawdown60, rsi, pe, warnings,
                    warningCount, estado, precioEntrada, precioActual,
                    precioResolucion, fechaResolucion, diasHastaResolucion,
                    pnlPct, diasTranscurridos, sourceFile,
                    fechaRegistroTimestamp, editadoManualmente,
                    ticker, fechaDeteccion
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, params)
            nuevas += 1

    conn.commit()
    return nuevas, actualizadas


def registro_delete_one(ticker: str, fecha: str):
    conn = get_turso_conn()
    conn.execute("DELETE FROM registro WHERE ticker = ? AND fechaDeteccion = ?", (ticker, fecha))
    conn.commit()


def registro_delete_all():
    conn = get_turso_conn()
    conn.execute("DELETE FROM registro")
    conn.commit()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/" or path == "":
            self.handle_root()
        elif path == "/status":
            self.handle_status()
        elif path == "/data":
            self.handle_data()
        elif path == "/market":
            self.handle_market()
        elif path == "/hot":
            self.handle_hot()
        elif path == "/mobile" or path == "/stock-radar-v3.html":
            self.serve_app()
        elif path == "/api/latest-csv":
            self.handle_latest_csv()
        elif path == "/api/registro":
            self.handle_registro_get()
        elif path == "/api/sidi/status":
            self.handle_sidi_status()
        elif path == "/api/sidi/market":
            self.handle_sidi_market()
        elif path == "/api/sidi/candidates":
            self.handle_sidi_candidates(query)
        elif path == "/api/sidi/work-packet":
            self.handle_sidi_work_packet(query)
        else:
            m = re.match(r"^/api/sidi/candidates/([^/]+)$", path)
            if m:
                self.handle_sidi_candidate(unquote(m.group(1)))
            else:
                super().do_GET()

    def do_POST(self):
        if self.path == "/trigger":
            self.handle_trigger()
        elif self.path == "/api/registro":
            self.handle_registro_post()
        else:
            self.send_error(404)

    def do_DELETE(self):
        m = re.match(r"^/api/registro/([^/]+)/([^/]+)$", self.path)
        if self.path == "/api/registro":
            self.handle_registro_delete_all()
        elif m:
            self.handle_registro_delete_one(m.group(1), m.group(2))
        else:
            self.send_error(404)

    # ── Rutas ────────────────────────────────────────────────────
    def serve_app(self):
        app_path = BASE_DIR / "stock-radar-v3.html"
        if not app_path.exists():
            self.send_json({"error": "stock-radar-v3.html no encontrado"}, status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        with open(app_path, "rb") as f:
            self.wfile.write(f.read())

    def handle_root(self):
        market, rows = load_market_context()
        self.send_json({
            "service": "STOCK-RADAR Cloud API",
            "status": "ok",
            "version": "1.2",
            "empresas": len(rows),
            "registro_backend": "turso" if turso_disponible() else "no configurado (usa localStorage)",
            "updated": datetime.now().isoformat(),
            "endpoints": [
                "/status", "/data", "/market", "/hot", "/trigger", "/mobile",
                "/api/latest-csv", "/api/registro (GET/POST/DELETE)",
                "/api/sidi/status", "/api/sidi/market", "/api/sidi/candidates",
                "/api/sidi/candidates/{ticker}", "/api/sidi/work-packet"
            ],
        })

    def handle_status(self):
        latest = get_latest_csv()
        market, rows = load_market_context()
        self.send_json({
            "ok": True,
            "empresas": len(rows),
            "csv_files": len(list(DATA_DIR.glob("sp500_full_export_*.csv"))) if DATA_DIR.exists() else 0,
            "latest_csv": latest.name if latest else None,
            "market": market.get("regime", "N/D"),
            "vix": market.get("vix", 0),
            "updated": datetime.now().isoformat(),
        })

    def handle_data(self):
        market, rows = load_market_context()
        self.send_json({"rows": rows, "count": len(rows)})

    def handle_market(self):
        market, _ = load_market_context()
        self.send_json(market)

    def handle_hot(self):
        market, rows = load_market_context()
        hot = [r for r in rows if str(r.get("setup_hot", "")).strip() == "True"]
        self.send_json({"hot": hot, "count": len(hot)})

    def handle_latest_csv(self):
        latest = get_latest_csv()
        if not latest:
            self.send_json({"error": "no_csv_found"}, status=404)
            return
        mtime = datetime.fromtimestamp(latest.stat().st_mtime)
        self.send_json({
            "filename": latest.name,
            "path": f"/data/master/{latest.name}",
            "modified": mtime.isoformat(),
            "modified_str": mtime.strftime("%d/%m/%Y %H:%M"),
            "size_kb": round(latest.stat().st_size / 1024, 1),
        })

    # ── SIDI Work API (solo lectura) ────────────────────────────
    def handle_sidi_status(self):
        latest = get_latest_csv()
        market, rows = load_market_context()
        _, hot_rows = _select_sidi_rows(rows, "hot")
        _, full_rows = _select_sidi_rows(rows, "full")
        self.send_json({
            "ok": True,
            "schema_version": "SIDI_WORK_PACKET_V1",
            "analysis_date": _analysis_date(rows),
            "latest_csv": latest.name if latest else None,
            "universe_count": len(rows),
            "setup_hot_count": len(hot_rows),
            "full_setup_count": len(full_rows),
            "market_regime": market.get("regime", "N/D"),
            "updated": datetime.now().isoformat(),
        })

    def handle_sidi_market(self):
        market, rows = load_market_context()
        self.send_json({
            "analysis_date": _analysis_date(rows),
            "market_context": market,
        })

    def handle_sidi_candidates(self, query):
        _, rows = load_market_context()
        scope = (query.get("scope", ["hot"])[0] or "hot").strip()
        normalized_scope, selected = _select_sidi_rows(rows, scope)
        candidates = []
        for r in selected:
            candidates.append({
                "ticker": r.get("ticker"),
                "name": r.get("name"),
                "sector": r.get("sector"),
                "industry": r.get("industry") or None,
                "fundamental_score": _float(r.get("fund_score")),
                "technical_score": _float(r.get("tech_score")),
                "combined_score": _float(r.get("combined_score")),
                "drawdown_60d_pct": abs(_float(r.get("drawdown_60d"), 0.0)),
                "rsi_14": _float(r.get("rsi_14")),
                "setup_hot": _bool(r.get("setup_hot")),
                "full_setup": _bool(r.get("full_setup")),
                "earnings_days_next": _int(r.get("earnings_days_next")),
            })
        self.send_json({
            "analysis_date": _analysis_date(rows),
            "selection_scope": normalized_scope,
            "count": len(candidates),
            "candidates": candidates,
        })

    def handle_sidi_candidate(self, ticker):
        _, rows = load_market_context()
        ticker = (ticker or "").strip().upper()
        row = next((r for r in rows if (r.get("ticker") or "").strip().upper() == ticker), None)
        if not row:
            self.send_json({"error": "ticker_not_found", "ticker": ticker}, status=404)
            return
        self.send_json({
            "schema_version": "SIDI_WORK_PACKET_V1",
            "analysis_date": _analysis_date(rows),
            "company": _row_to_sidi_company(row),
        })

    def handle_sidi_work_packet(self, query):
        _, rows = load_market_context()
        scope = (query.get("scope", ["hot"])[0] or "hot").strip()
        tickers_raw = query.get("tickers", [""])[0]
        tickers = [t for t in tickers_raw.split(",") if t.strip()] if tickers_raw else None
        packet = _build_work_packet(rows, scope=scope, tickers=tickers)
        self.send_json(packet)

    def handle_trigger(self):
        token = self.headers.get("X-Update-Token", "")
        if token != UPDATE_TOKEN:
            self.send_json({"ok": False, "error": "token invalido"}, status=401)
            return

        def run_ingesta():
            inicio = datetime.now()
            print(f"  🚀 Lanzando main_ingesta.py en background ({inicio.strftime('%H:%M:%S')})...", flush=True)
            try:
                resultado = subprocess.run(
                    [sys.executable, str(BASE_DIR / "main_ingesta.py")],
                    cwd=str(BASE_DIR), timeout=3600,
                )
                duracion = (datetime.now() - inicio).total_seconds()
                if resultado.returncode == 0:
                    print(f"  ✅ Ingesta completada correctamente en {duracion:.0f}s", flush=True)
                else:
                    print(f"  ❌ Ingesta terminó con returncode={resultado.returncode} tras {duracion:.0f}s "
                          f"({'posible OOM-kill / límite de memoria' if resultado.returncode < 0 else 'ver traceback arriba'})",
                          flush=True)
            except subprocess.TimeoutExpired:
                duracion = (datetime.now() - inicio).total_seconds()
                print(f"  ❌ Ingesta cancelada por timeout tras {duracion:.0f}s (límite: 3600s)", flush=True)
            except Exception as e:
                duracion = (datetime.now() - inicio).total_seconds()
                print(f"  ❌ Error en ingesta background tras {duracion:.0f}s: {e}", flush=True)

        threading.Thread(target=run_ingesta, daemon=True).start()
        self.send_json({"ok": True, "message": "Actualización iniciada en background"})

    def handle_registro_get(self):
        if not turso_disponible():
            self.send_json({"ok": False, "error": "turso_not_configured",
                             "message": "TURSO_DATABASE_URL / TURSO_AUTH_TOKEN no configuradas en el servidor."}, status=503)
            return
        try:
            señales = registro_get_all()
            self.send_json({"ok": True, "registro": señales, "count": len(señales)})
        except Exception as e:
            print(f"  ❌ Error leyendo registro de Turso: {e}")
            self.send_json({"ok": False, "error": str(e)}, status=500)

    def handle_registro_post(self):
        if not turso_disponible():
            self.send_json({"ok": False, "error": "turso_not_configured",
                             "message": "TURSO_DATABASE_URL / TURSO_AUTH_TOKEN no configuradas en el servidor."}, status=503)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(body.decode("utf-8"))

            señales = payload.get("señales") or payload.get("registro") or payload
            if isinstance(señales, dict):
                señales = [señales]
            if not isinstance(señales, list):
                self.send_json({"ok": False, "error": "Se esperaba una lista de señales"}, status=400)
                return

            nuevas, actualizadas = registro_upsert_many(señales)
            self.send_json({"ok": True, "nuevas": nuevas, "actualizadas": actualizadas})
        except json.JSONDecodeError:
            self.send_json({"ok": False, "error": "JSON inválido"}, status=400)
        except Exception as e:
            print(f"  ❌ Error escribiendo registro en Turso: {e}")
            self.send_json({"ok": False, "error": str(e)}, status=500)

    def handle_registro_delete_one(self, ticker, fecha):
        if not turso_disponible():
            self.send_json({"ok": False, "error": "turso_not_configured"}, status=503)
            return
        try:
            registro_delete_one(ticker, fecha)
            self.send_json({"ok": True})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, status=500)

    def handle_registro_delete_all(self):
        if not turso_disponible():
            self.send_json({"ok": False, "error": "turso_not_configured"}, status=503)
            return
        try:
            registro_delete_all()
            self.send_json({"ok": True})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, status=500)

    # ── Utilidades ───────────────────────────────────────────────
    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
   App:     {url}
   Datos:   {DATA_DIR}
   Modo:    {'Render' if IS_RENDER else 'Local'}
   Registro: {'Turso conectado' if turso_disponible() else '⚠ Turso NO configurado (TURSO_DATABASE_URL/TURSO_AUTH_TOKEN faltan)'}
  ================================================
""")
        if not IS_RENDER:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Servidor detenido.")


if __name__ == "__main__":
    main()
