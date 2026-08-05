"""
STOCK-RADAR · Cloud API Server
Flask app para Railway/Render — sirve datos a la app móvil

Endpoints:
  GET /              → status
  GET /status        → health check
  GET /data          → CSV completo como JSON
  GET /market        → contexto de mercado (SPY + VIX)
  GET /hot           → solo setups HOT filtrados
  POST /trigger      → fuerza actualización de datos (protegido por token)
"""

import os
import json
import csv
import glob
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, abort
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DATA_DIR   = Path(__file__).parent / "data" / "master"
UPDATE_TOKEN = os.environ.get("UPDATE_TOKEN", "stock-radar-2026")

# ── Cache en memoria para respuestas rápidas ─────────────────────
_cache = {
    'data':    None,
    'market':  None,
    'updated': None,
}

def load_latest_csv():
    """Carga el CSV más reciente en memoria."""
    csvs = sorted(DATA_DIR.glob("sp500_full_export_*.csv"))
    if not csvs:
        return []
    latest = csvs[-1]
    rows = []
    with open(latest, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows

def load_market_context():
    """Carga el market_context.json."""
    ctx_path = DATA_DIR / "market_context.json"
    if ctx_path.exists():
        with open(ctx_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def refresh_cache():
    """Recarga la cache desde disco."""
    try:
        _cache['data']    = load_latest_csv()
        _cache['market']  = load_market_context()
        _cache['updated'] = datetime.now().isoformat()
        print(f"  Cache actualizada: {len(_cache['data'])} empresas")
    except Exception as e:
        print(f"  Error recargando cache: {e}")

# Cargar cache al arrancar
refresh_cache()


@app.route('/')
def index():
    return jsonify({
        'service':  'STOCK-RADAR Cloud API',
        'version':  '1.0',
        'status':   'ok',
        'updated':  _cache.get('updated'),
        'empresas': len(_cache.get('data') or []),
        'endpoints': ['/status', '/data', '/market', '/hot', '/trigger'],
    })


@app.route('/status')
def status():
    csvs = sorted(DATA_DIR.glob("sp500_full_export_*.csv"))
    return jsonify({
        'ok':          True,
        'updated':     _cache.get('updated'),
        'empresas':    len(_cache.get('data') or []),
        'csv_files':   len(csvs),
        'latest_csv':  csvs[-1].name if csvs else None,
        'market':      _cache.get('market', {}).get('market_regime', 'N/D'),
        'vix':         _cache.get('market', {}).get('vix', 0),
    })


@app.route('/market')
def market():
    """Contexto de mercado: SPY + VIX + recomendación."""
    ctx = _cache.get('market') or load_market_context()
    if not ctx:
        return jsonify({'error': 'Sin datos de mercado'}), 404
    return jsonify(ctx)


@app.route('/data')
def data():
    """CSV completo como JSON. Soporta filtros por query params."""
    rows = _cache.get('data')
    if rows is None:
        rows = load_latest_csv()

    # Filtros opcionales
    min_score = float(request.args.get('min_score', 0))
    min_dd    = float(request.args.get('min_dd', 0))
    sector    = request.args.get('sector', '')
    hot_only  = request.args.get('hot_only', 'false').lower() == 'true'

    filtered = rows
    if min_score > 0:
        filtered = [r for r in filtered
                    if _safe_float(r.get('fund_score') or r.get('combined_score')) >= min_score]
    if min_dd > 0:
        filtered = [r for r in filtered
                    if abs(_safe_float(r.get('drawdown_60d'))) >= min_dd]
    if sector:
        filtered = [r for r in filtered
                    if sector.lower() in (r.get('sector') or '').lower()]
    if hot_only:
        filtered = [r for r in filtered
                    if str(r.get('setup_hot')).lower() in ('true', '1')]

    return jsonify({
        'total':    len(rows),
        'filtered': len(filtered),
        'updated':  _cache.get('updated'),
        'data':     filtered,
    })


@app.route('/hot')
def hot():
    """Solo setups HOT con score >= 6.5 y DD >= 12%."""
    rows = _cache.get('data')
    if rows is None:
        rows = load_latest_csv()

    min_score = float(request.args.get('min_score', 6.5))
    min_dd    = float(request.args.get('min_dd', 12.0))

    hot_rows = [r for r in rows
                if _safe_float(r.get('fund_score') or r.get('combined_score')) >= min_score
                and abs(_safe_float(r.get('drawdown_60d'))) >= min_dd]

    # Ordenar por score descendente
    hot_rows.sort(key=lambda r: _safe_float(r.get('fund_score') or r.get('combined_score')), reverse=True)

    return jsonify({
        'count':   len(hot_rows),
        'updated': _cache.get('updated'),
        'market':  _cache.get('market', {}).get('market_regime', 'N/D'),
        'vix':     _cache.get('market', {}).get('vix', 0),
        'data':    hot_rows,
    })


@app.route('/trigger', methods=['POST'])
def trigger_update():
    """Fuerza actualización de datos. Requiere token."""
    token = request.headers.get('X-Update-Token') or request.json.get('token', '')
    if token != UPDATE_TOKEN:
        abort(401)

    def run_ingesta():
        try:
            print("  Iniciando ingesta...")
            subprocess.run(
                ['python', 'main_ingesta.py'],
                cwd=str(Path(__file__).parent),
                timeout=3600,
                check=True
            )
            refresh_cache()
            print("  Ingesta completada")
        except Exception as e:
            print(f"  Error en ingesta: {e}")

    thread = threading.Thread(target=run_ingesta, daemon=True)
    thread.start()

    return jsonify({'ok': True, 'message': 'Actualización iniciada en background'})


@app.route('/refresh-cache', methods=['POST'])
def refresh():
    """Recarga la cache desde disco (sin re-ejecutar ingesta)."""
    refresh_cache()
    return jsonify({'ok': True, 'empresas': len(_cache.get('data') or [])})


def _safe_float(val, default=0.0):
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return default


@app.route('/favicon.ico')
def favicon():
    from flask import send_from_directory
    return send_from_directory('static', 'icon.png')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

# Arrancar scheduler si está en producción
import os as _os
if _os.environ.get('RAILWAY_ENVIRONMENT') or _os.environ.get('RENDER'):
    try:
        from scheduler import start_scheduler
        _scheduler = start_scheduler()
    except Exception as _e:
        print(f"  Scheduler no iniciado: {_e}")


@app.route('/desktop')
@app.route('/app')
def desktop():
    """App de escritorio completa servida desde el cloud."""
    from flask import send_file
    desktop_path = Path(__file__).parent / 'static' / 'desktop.html'
    if desktop_path.exists():
        return send_file(str(desktop_path))
    return jsonify({'error': 'Desktop app not found'}), 404

@app.route('/mobile')
def mobile():
    """App móvil PWA."""
    from flask import render_template
    return render_template('mobile.html')

@app.route('/static/<path:filename>')
def static_files(filename):
    from flask import send_from_directory
    return send_from_directory('static', filename)
