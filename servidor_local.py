"""
SIDI STOCKS - Servidor de despliegue (local y Render)
=======================================================
Sirve stock-radar-v3.html y expone la API de datos que ya usaba el
sistema (status, data, market, hot, trigger) + el endpoint auxiliar
/api/latest-csv que la app usa para auto-cargar el CSV mas reciente
cuando corre con este servidor (en vez de doble clic al HTML suelto).

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
import threading
import webbrowser
import subprocess
from pathlib import Path
from datetime import datetime

PORT = int(os.environ.get("PORT", 8000))
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data" / "master"
UPDATE_TOKEN = os.environ.get("UPDATE_TOKEN", "stock-radar-2026")
IS_RENDER = os.environ.get("RENDER", "").lower() == "true" or "RENDER" in os.environ


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


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def do_GET(self):
        if self.path == "/" or self.path == "":
            self.handle_root()
        elif self.path == "/status":
            self.handle_status()
        elif self.path == "/data":
            self.handle_data()
        elif self.path == "/market":
            self.handle_market()
        elif self.path == "/hot":
            self.handle_hot()
        elif self.path == "/mobile" or self.path == "/stock-radar-v3.html":
            self.serve_app()
        elif self.path == "/api/latest-csv":
            self.handle_latest_csv()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/trigger":
            self.handle_trigger()
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
            "version": "1.0",
            "empresas": len(rows),
            "updated": datetime.now().isoformat(),
            "endpoints": ["/status", "/data", "/market", "/hot", "/trigger", "/mobile", "/api/latest-csv"],
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

    def handle_trigger(self):
        token = self.headers.get("X-Update-Token", "")
        if token != UPDATE_TOKEN:
            self.send_json({"ok": False, "error": "token invalido"}, status=401)
            return

        def run_ingesta():
            try:
                subprocess.run(
                    [sys.executable, str(BASE_DIR / "main_ingesta.py")],
                    cwd=str(BASE_DIR), timeout=3600,
                )
            except Exception as e:
                print(f"  ❌ Error en ingesta background: {e}")

        threading.Thread(target=run_ingesta, daemon=True).start()
        self.send_json({"ok": True, "message": "Actualización iniciada en background"})

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
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        httpd.allow_reuse_address = True
        url = f"http://localhost:{PORT}/stock-radar-v3.html"
        print(f"""
  ================================================
   SIDI STOCKS - Servidor activo (puerto {PORT})
  ================================================
   App:   {url}
   Datos: {DATA_DIR}
   Modo:  {'Render' if IS_RENDER else 'Local'}
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
