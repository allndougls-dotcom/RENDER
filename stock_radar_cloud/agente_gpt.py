"""
SIDI STOCKS · Agente GPT News — Inteligencia Empresarial
Segundo agente: noticias, earnings call, analistas, sector y competidores.
Variables: OPENAI_API_KEY, TAVILY_API_KEY, ALERT_EMAIL_TO/FROM/PASS
"""
import os, json, time, smtplib, requests
from pathlib import Path
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "")
EMAIL_TO   = os.environ.get("ALERT_EMAIL_TO",  "allndougls@gmail.com")
EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", "")
EMAIL_PASS = os.environ.get("ALERT_EMAIL_PASS", "")
DATA_DIR   = Path(__file__).parent / "data" / "master"
MAX_EMPRESAS, DELAY = 6, 4

SECTOR_ETF = {"Information Technology":"XLK","Health Care":"XLV","Financials":"XLF",
    "Consumer Discretionary":"XLY","Consumer Staples":"XLP","Energy":"XLE","Materials":"XLB",
    "Industrials":"XLI","Utilities":"XLU","Real Estate":"XLRE","Communication Services":"XLC"}
SECTOR_PEERS = {"Information Technology":["AAPL","MSFT","NVDA","AVGO","AMD"],
    "Health Care":["JNJ","UNH","LLY","ABBV","MRK"],"Financials":["JPM","BAC","WFC","GS","MS"],
    "Consumer Discretionary":["AMZN","TSLA","HD","MCD","NKE"],"Consumer Staples":["PG","KO","PEP","WMT","COST"],
    "Energy":["XOM","CVX","COP","EOG","SLB"],"Materials":["LIN","APD","ECL","SHW","NEM"],
    "Industrials":["HON","UPS","CAT","DE","RTX"],"Communication Services":["GOOGL","META","NFLX","DIS","T"]}


def load_hot_companies():
    import csv
    csvs = sorted(DATA_DIR.glob("sp500_full_export_*.csv"))
    if not csvs: return [], {}
    companies = []
    with open(csvs[-1], 'r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            try:
                score = float(row.get('fund_score') or row.get('combined_score') or 0)
                dd = abs(float(row.get('drawdown_60d') or 0))
                rsi = float(row.get('rsi_14') or row.get('rsi') or 50)
                earnings = int(row.get('earnings_days_next') or 999)
                if score >= 6.5 and dd >= 12 and rsi < 40 and earnings > 7:
                    companies.append(row)
            except Exception: continue
    companies.sort(key=lambda r: float(r.get('fund_score') or r.get('combined_score') or 0), reverse=True)
    market = {}
    ctx = DATA_DIR / "market_context.json"
    if ctx.exists():
        with open(ctx) as f: market = json.load(f)
    return companies[:MAX_EMPRESAS], market


def search_tavily(query, max_results=4, domains=None):
    if not TAVILY_KEY: return []
    try:
        payload = {"api_key": TAVILY_KEY, "query": query, "search_depth": "advanced",
            "include_answer": True, "include_raw_content": False, "max_results": max_results}
        if domains: payload["include_domains"] = domains
        r = requests.post("https://api.tavily.com/search", headers={"Content-Type":"application/json"},
            json=payload, timeout=20)
        if r.status_code == 200:
            data = r.json()
            results = []
            if data.get("answer"): results.append({"type":"answer","content":data["answer"]})
            for item in data.get("results", [])[:max_results]:
                results.append({"type":"article","title":item.get("title",""),
                    "content":item.get("content","")[:400],"date":item.get("published_date","")[:10]})
            return results
    except Exception as e:
        print(f"    Tavily error: {e}")
    return []


def gather_intelligence(ticker, name, sector):
    intel = {}
    domains = ["reuters.com","bloomberg.com","wsj.com","cnbc.com","marketwatch.com",
               "seekingalpha.com","barrons.com","finance.yahoo.com","fool.com","benzinga.com"]
    intel["noticias"] = search_tavily(f"{ticker} {name} stock news 2025 2026 earnings guidance", 4, domains)
    time.sleep(1)
    intel["earnings_call"] = search_tavily(f"{ticker} {name} earnings call transcript CEO CFO guidance", 3, domains)
    time.sleep(1)
    intel["analistas"] = search_tavily(f"{ticker} analyst rating upgrade downgrade price target", 3, domains)
    time.sleep(1)
    etf = SECTOR_ETF.get(sector, "SPY")
    peers = " ".join(SECTOR_PEERS.get(sector, [])[:3])
    intel["sector"] = search_tavily(f"{etf} {sector} sector outlook {peers}", 3, domains)
    time.sleep(1)
    intel["riesgos"] = search_tavily(f"{ticker} {name} risk lawsuit regulatory debt dividend", 3, domains)
    return intel


def format_intel_for_gpt(ticker, name, sector, price, dd, rsi, score, intel, market, company_row):
    now = datetime.now().strftime("%Y-%m-%d")
    etf = SECTOR_ETF.get(sector, "N/D")
    peers = ", ".join(SECTOR_PEERS.get(sector, [])[:4])
    mkt_regime, vix = market.get("market_regime","N/D"), market.get("vix", 0)

    def fmt_results(results, max_items=3):
        if not results: return "Sin datos disponibles"
        lines = []
        for r in results[:max_items]:
            if r["type"] == "answer": lines.append(f"RESUMEN: {r['content'][:300]}")
            else: lines.append(f"• [{r.get('date','')}] {r['title']}: {r['content'][:250]}")
        return "\n".join(lines) if lines else "Sin datos"

    def sf(v, d=2):
        try: return round(float(v or 0), d)
        except: return 0

    prompt = f"""SIDI NEWS CONTEXT ANALYZER — ANÁLISIS PROFUNDO
Fecha: {now} | Mercado: {mkt_regime} | VIX: {vix:.1f}
Estrategia: Mean Reversion S&P500 | Horizonte: 8-15 días

EMPRESA: {ticker} | {name} | {sector}
Precio: ${price:.2f} | Caída 60d: -{dd:.1f}% | RSI: {rsi:.1f} | Score SIDI: {score:.2f}/10
Sector ETF: {etf} | Competidores: {peers}

[A] NOTICIAS RECIENTES:
{fmt_results(intel.get('noticias', []))}

[B] EARNINGS CALL:
{fmt_results(intel.get('earnings_call', []))}

[C] ANALISTAS:
{fmt_results(intel.get('analistas', []))}

[D] SECTOR Y COMPETIDORES:
{fmt_results(intel.get('sector', []))}

[E] RIESGOS:
{fmt_results(intel.get('riesgos', []))}

INSTRUCCIONES — Responde:
1. DIAGNÓSTICO RÁPIDO (tipo caída, gravedad, exagerada o justificada)
2. CONTEXTO SECTOR (vs pares y mercado)
3. POR QUÉ CAYÓ (causa y si sigue vigente)
4. EARNINGS Y MANAGEMENT (resultado, guidance, comentario CEO)
5. NOTICIAS CLASIFICADAS POR IMPACTO
6. COMPARATIVA COMPETIDORES
7. CATALIZADORES (rebote y deterioro)
8. RIESGOS Y PENALIZADORES
9. SCORING 0-10 por bloque + SCORE FINAL AJUSTADO
10. VEREDICTO: FAVORABLE/NEUTRO/DESFAVORABLE
11. CONCLUSIÓN OPERATIVA

Responde en español, específico con fechas y cifras.
Al final incluye EXACTAMENTE:
GPT_VEREDICTO: [FAVORABLE/NEUTRO/DESFAVORABLE]
GPT_SCORE: [X.X]
GPT_TIPO_CAIDA: [tipo]
GPT_CONFIANZA: [X]/10"""
    return prompt


def call_openai(prompt):
    if not OPENAI_KEY:
        print("  ⚠ Sin OPENAI_API_KEY")
        return None
    try:
        r = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o", "max_tokens": 2000, "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": "Eres el analista de inteligencia empresarial de SIDI STOCKS. Sigue exactamente el formato solicitado. Responde en español."},
                    {"role": "user", "content": prompt}]},
            timeout=60)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        print(f"    ❌ OpenAI error {r.status_code}: {r.text[:100]}")
        return None
    except Exception as e:
        print(f"    ❌ OpenAI error: {e}")
        return None


def parse_gpt_response(text):
    if not text: return "NEUTRO", 5.0, "desconocida", 5.0
    veredicto, score, tipo, confianza = "NEUTRO", 5.0, "desconocida", 5.0
    for line in text.upper().split("\n"):
        line = line.strip()
        if line.startswith("GPT_VEREDICTO:"):
            v = line.replace("GPT_VEREDICTO:", "").strip()
            veredicto = "FAVORABLE" if "FAVORABLE" in v else "DESFAVORABLE" if "DESFAVORABLE" in v else "NEUTRO"
        elif line.startswith("GPT_SCORE:"):
            try: score = float(line.replace("GPT_SCORE:", "").strip())
            except: pass
        elif line.startswith("GPT_TIPO_CAIDA:"):
            tipo = line.replace("GPT_TIPO_CAIDA:", "").strip().lower()
        elif line.startswith("GPT_CONFIANZA:"):
            try: confianza = float(line.replace("GPT_CONFIANZA:", "").strip().split("/")[0])
            except: pass
    return veredicto, score, tipo, confianza


def send_email(subject, html_body, plain_body=""):
    if not EMAIL_FROM or not EMAIL_PASS:
        print(f"  ⚠ Sin credenciales email\n  {subject}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subject, f"SIDI STOCKS News <{EMAIL_FROM}>", EMAIL_TO
        if plain_body: msg.attach(MIMEText(plain_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as s:
            s.login(EMAIL_FROM, EMAIL_PASS)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"  ✅ Email enviado a {EMAIL_TO}")
        return True
    except Exception as e:
        print(f"  ❌ Email error: {e}")
        return False


def format_gpt_email(ticker, company_row, gpt_response, veredicto, score, tipo, confianza, market):
    def sf(v, d=2):
        try: return round(float(v or 0), d)
        except: return 0
    name, sector = company_row.get("name", ticker), company_row.get("sector", "?")
    price, dd = sf(company_row.get("price"), 2), abs(sf(company_row.get("drawdown_60d"), 1))
    fund_score = sf(company_row.get("fund_score") or company_row.get("combined_score"))
    mkt, vix, now = market.get("market_regime","?"), market.get("vix", 0), datetime.now().strftime("%d/%m/%Y %H:%M")
    color_map = {"FAVORABLE":"#22c55e","NEUTRO":"#f59e0b","DESFAVORABLE":"#ef4444"}
    icon_map = {"FAVORABLE":"📰✅","NEUTRO":"📰⚠️","DESFAVORABLE":"📰❌"}
    color, icon = color_map.get(veredicto,"#94a3b8"), icon_map.get(veredicto,"📰")

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{font-family:'Segoe UI',Arial,sans-serif;background:#07090f;color:#e2e8f0;margin:0;padding:0}}
.c{{max-width:620px;margin:0 auto;padding:20px}} .h{{background:linear-gradient(135deg,#0D1B2A,#1a2a3a);border-radius:10px;padding:22px;margin-bottom:14px;border:1px solid #1e3a5f}}
.card{{background:#0d1017;border:1px solid #1a1e2c;border-radius:8px;padding:14px;margin-bottom:10px}}
.label{{font-size:9px;color:#64748b;letter-spacing:2px;text-transform:uppercase;margin-bottom:3px}}
.val{{font-size:16px;font-weight:700;color:#fff}} .grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px}}
.analysis{{background:#0a0f1a;border:1px solid #1e3a5f;border-radius:6px;padding:14px;font-size:11px;line-height:1.7;color:#94a3b8;font-family:monospace;white-space:pre-wrap;max-height:500px;overflow-y:auto}}
.footer{{font-size:10px;color:#475569;text-align:center;margin-top:16px}}</style></head>
<body><div class="c">
  <div class="h">
    <div style="font-size:20px;font-weight:900;color:#fff;letter-spacing:3px">⚔ SIDI·STOCKS</div>
    <div style="font-size:13px;color:#94a3b8;margin-top:6px">SIDI News Analyzer · {now}</div>
    <div style="font-size:12px;color:#64748b">{mkt} · VIX {vix:.1f}</div>
    <div style="margin-top:10px">
      <span style="background:{color}22;color:{color};border:1px solid {color}44;padding:4px 14px;border-radius:4px;font-weight:700;font-size:13px">{icon} {veredicto}</span>
      <span style="margin-left:8px;color:#94a3b8;font-size:11px">Score {score:.1f}/10 · Confianza {confianza:.0f}/10</span>
    </div>
  </div>
  <div class="card" style="border-left:4px solid {color}">
    <div style="display:flex;justify-content:space-between;margin-bottom:12px">
      <div><div style="font-size:26px;font-weight:900;color:#fff">{ticker}</div><div style="font-size:12px;color:#64748b">{name} · {sector}</div></div>
      <div style="text-align:right"><div style="font-size:22px;font-weight:700;color:#fff">${price}</div><div style="color:#ef4444;font-size:11px">-{dd}% en 60d</div></div>
    </div>
    <div class="grid">
      <div><div class="label">Score SIDI</div><div class="val" style="color:{'#22c55e' if fund_score>=7 else '#f59e0b'}">{fund_score}</div></div>
      <div><div class="label">News Score</div><div class="val" style="color:{color}">{score:.1f}/10</div></div>
      <div><div class="label">Tipo caída</div><div class="val" style="font-size:11px;color:#94a3b8">{tipo}</div></div>
    </div>
  </div>
  <div class="card"><div class="label" style="margin-bottom:10px">Análisis SIDI News — GPT-4o</div><div class="analysis">{gpt_response}</div></div>
  <div class="footer">SIDI STOCKS · SIDI News Analyzer · GPT-4o + Tavily<br>No es consejo financiero.</div>
</div></body></html>"""

    plain = f"""SIDI STOCKS · SIDI News Analyzer
{now}
{ticker} — {name} ({sector})
Precio: ${price} | Score SIDI: {fund_score} | DD: -{dd}%
News Score: {score:.1f}/10 | Tipo: {tipo} | Confianza: {confianza:.0f}/10
VEREDICTO: {icon} {veredicto}

--- ANÁLISIS GPT ---
{gpt_response[:1500]}

---
SIDI STOCKS · Mean Reversion S&P500"""
    return html, plain


def run_gpt_agent():
    print(f"\n{'='*60}\n  SIDI News Analyzer · GPT-4o — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*60}")
    if not OPENAI_KEY:
        print("  ⚠ Sin OPENAI_API_KEY — agente GPT desactivado")
        return []

    hot_companies, market = load_hot_companies()
    if not hot_companies:
        print("  Sin HOTs hoy")
        return []

    resultados = []
    for i, company in enumerate(hot_companies):
        ticker, name, sector = company.get("ticker","?"), company.get("name",""), company.get("sector","?")
        score = round(float(company.get("fund_score") or company.get("combined_score") or 0), 2)
        dd = abs(round(float(company.get("drawdown_60d") or 0), 1))
        rsi = round(float(company.get("rsi_14") or company.get("rsi") or 50), 1)
        price = round(float(company.get("price") or 0), 2)

        print(f"\n  [{i+1}/{len(hot_companies)}] {ticker} — Score {score} DD -{dd}%")
        intel = gather_intelligence(ticker, name, sector)
        prompt = format_intel_for_gpt(ticker, name, sector, price, dd, rsi, score, intel, market, company)
        print(f"    Analizando con GPT-4o...")
        gpt_resp = call_openai(prompt)
        if not gpt_resp: continue

        veredicto, news_score, tipo, confianza = parse_gpt_response(gpt_resp)
        print(f"    GPT: {veredicto} · Score {news_score:.1f}/10 · Confianza {confianza:.0f}/10")

        resultados.append({"ticker":ticker,"name":name,"sector":sector,"score":score,"dd":dd,"rsi":rsi,
            "veredicto":veredicto,"news_score":news_score,"tipo":tipo,"confianza":confianza,"analysis":gpt_resp})

        if veredicto == "FAVORABLE" and confianza >= 7:
            html, plain = format_gpt_email(ticker, company, gpt_resp, veredicto, news_score, tipo, confianza, market)
            send_email(subject=f"📰 SIDI News · FAVORABLE: {ticker} · {news_score:.1f}/10 · Confianza {confianza:.0f}/10",
                html_body=html, plain_body=plain)

        if i < len(hot_companies) - 1:
            time.sleep(DELAY)

    if resultados:
        _send_summary(resultados, market)

    print(f"\n  Agente GPT completado — {len(resultados)} empresas")
    print(f"{'='*60}\n")
    return resultados


def _send_summary(resultados, market):
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    mkt, vix = market.get("market_regime","?"), market.get("vix", 0)
    favs = [r for r in resultados if r["veredicto"] == "FAVORABLE"]
    neuts = [r for r in resultados if r["veredicto"] == "NEUTRO"]
    desf = [r for r in resultados if r["veredicto"] == "DESFAVORABLE"]

    rows = "".join(f"""<tr style="border-bottom:1px solid #1a1e2c">
        <td style="padding:7px 10px;font-weight:700;color:#fff">{r['ticker']}</td>
        <td style="padding:7px 10px;color:#94a3b8;font-size:10px">{r['name'][:18]}</td>
        <td style="padding:7px 10px;text-align:center;color:{'#22c55e' if r['veredicto']=='FAVORABLE' else '#f59e0b' if r['veredicto']=='NEUTRO' else '#ef4444'};font-weight:700">{r['veredicto']}</td>
        <td style="padding:7px 10px;text-align:center;color:#fff">{r['news_score']:.1f}</td>
        <td style="padding:7px 10px;text-align:center;color:#94a3b8;font-size:10px">{r['tipo'][:15]}</td>
        <td style="padding:7px 10px;text-align:center;color:#64748b">{r['confianza']:.0f}/10</td>
      </tr>""" for r in resultados)

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{font-family:'Segoe UI',Arial,sans-serif;background:#07090f;color:#e2e8f0;padding:20px}}
.c{{max-width:620px;margin:0 auto}} table{{width:100%;border-collapse:collapse;background:#0d1017;border-radius:8px;overflow:hidden}}
th{{background:#0D1B2A;padding:9px 10px;font-size:9px;color:#64748b;letter-spacing:2px;text-transform:uppercase;text-align:left}}</style></head>
<body><div class="c">
  <div style="background:linear-gradient(135deg,#0D1B2A,#1a2a3a);border-radius:10px;padding:20px;margin-bottom:14px;border:1px solid #1e3a5f">
    <div style="font-size:20px;font-weight:900;color:#fff;letter-spacing:3px">⚔ SIDI·STOCKS</div>
    <div style="font-size:12px;color:#94a3b8;margin-top:6px">SIDI News · Resumen · {now}</div>
    <div style="font-size:11px;color:#64748b">{mkt} · VIX {vix:.1f}</div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:14px">
    <div style="background:#0d1017;border:1px solid rgba(34,197,94,0.2);border-radius:8px;padding:12px;text-align:center"><div style="font-size:26px;font-weight:900;color:#22c55e">{len(favs)}</div><div style="font-size:9px;color:#64748b;letter-spacing:2px">FAVORABLE</div></div>
    <div style="background:#0d1017;border:1px solid rgba(245,158,11,0.2);border-radius:8px;padding:12px;text-align:center"><div style="font-size:26px;font-weight:900;color:#f59e0b">{len(neuts)}</div><div style="font-size:9px;color:#64748b;letter-spacing:2px">NEUTRO</div></div>
    <div style="background:#0d1017;border:1px solid rgba(239,68,68,0.2);border-radius:8px;padding:12px;text-align:center"><div style="font-size:26px;font-weight:900;color:#ef4444">{len(desf)}</div><div style="font-size:9px;color:#64748b;letter-spacing:2px">DESFAVORABLE</div></div>
  </div>
  <table><tr><th>Ticker</th><th>Empresa</th><th>Veredicto</th><th>Score</th><th>Tipo</th><th>Confianza</th></tr>{rows}</table>
  <div style="font-size:10px;color:#475569;text-align:center;margin-top:16px">SIDI STOCKS · SIDI News Analyzer · No es consejo financiero</div>
</div></body></html>"""

    subject = (f"📰 SIDI News · {len(favs)} FAVORABLE{'s' if len(favs)>1 else ''} hoy · {now.split()[0]}"
               if favs else f"📰 SIDI News · Resumen {now.split()[0]} — Sin FAVORABLEs")
    send_email(subject=subject, html_body=html)


if __name__ == "__main__":
    run_gpt_agent()
