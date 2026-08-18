"""
╔══════════════════════════════════════════════════════╗
║  STOCK-RADAR · Proxy OpenAI                         ║
║                                                      ║
║  Permite que la app HTML llame a OpenAI sin CORS.   ║
║                                                      ║
║  Uso:                                                ║
║    python proxy_openai.py                           ║
║                                                      ║
║  Mantén esta terminal abierta mientras usas          ║
║  el AI·Preparator con GPT-4o.                       ║
╚══════════════════════════════════════════════════════╝
"""

import json
import http.server
import urllib.request
import urllib.error


PORT = 8765


class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # Log limpio sin spam
        if '200' in str(args) or 'Error' in str(args):
            print(f"  [{args[0]}] {args[1]}")

    def do_OPTIONS(self):
        # CORS preflight
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_POST(self):
        if self.path != '/openai':
            self.send_error(404)
            return

        # Leer body
        length = int(self.headers.get('Content-Length', 0))
        body   = json.loads(self.rfile.read(length))

        api_key    = body.get('apiKey', '')
        prompt     = body.get('prompt', '')
        web_search = body.get('webSearch', True)

        if not api_key:
            self._json_error(400, 'API key no proporcionada')
            return

        # Construir request a OpenAI
        tools = []
        if web_search:
            tools = [{"type": "web_search_preview"}]

        payload = {
            "model": "gpt-4o-mini-search-preview" if web_search else "gpt-4o-mini",
            "messages": [
                {
                    "role": "system",
                    "content": "Eres un analista financiero experto en swing trading del S&P500. Busca información actualizada cuando sea necesario. Responde siempre en español."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": 3000,
        }

        if tools:
            payload["tools"] = tools

        req = urllib.request.Request(
            'https://api.openai.com/v1/chat/completions',
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type':  'application/json',
                'Authorization': f'Bearer {api_key}',
            },
            method='POST'
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                data = json.loads(response.read())
                text = data['choices'][0]['message']['content']

                self.send_response(200)
                self._cors_headers()
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'text': text}).encode('utf-8'))
                print(f"  ✅ GPT-4o respondió ({len(text)} chars)")

        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            try:
                error_data = json.loads(error_body)
                msg = error_data.get('error', {}).get('message', error_body)
            except Exception:
                msg = error_body
            print(f"  ❌ OpenAI error {e.code}: {msg[:100]}")
            self._json_error(e.code, msg)

        except Exception as e:
            print(f"  ❌ Error: {e}")
            self._json_error(500, str(e))

    def _cors_headers(self):
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _json_error(self, code, message):
        self.send_response(code)
        self._cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'error': message}).encode('utf-8'))


if __name__ == '__main__':
    server = http.server.HTTPServer(('localhost', PORT), ProxyHandler)
    print(f"""
╔══════════════════════════════════════════════╗
║  STOCK-RADAR · Proxy OpenAI activo          ║
╠══════════════════════════════════════════════╣
║  Puerto : {PORT}                              ║
║  URL    : http://localhost:{PORT}/openai       ║
╠══════════════════════════════════════════════╣
║  Mantén esta terminal abierta.              ║
║  Ctrl+C para detener.                       ║
╚══════════════════════════════════════════════╝
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Proxy detenido.")
        server.server_close()
