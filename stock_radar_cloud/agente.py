"""
SIDI STOCKS · Agente de Análisis Autónomo (Claude)
=========================================
Flujo diario: carga HOTs del CSV, genera informe cuantitativo,
llama a Claude API, y si EXECUTE envía alerta por email.
Bloquea EXECUTE si el veredicto de noticias es DESFAVORABLE.

Variables de entorno necesarias en el hosting:
  ANTHROPIC_API_KEY   → API key de Anthropic
  TAVILY_API_KEY      → API key de Tavily (búsqueda de noticias)
  ALERT_EMAIL_TO      → email destino
  ALERT_EMAIL_FROM    → Gmail origen
  ALERT_EMAIL_PASS    → contraseña de aplicación Gmail
"""

import os
import csv
import json
import time
import smtplib
import requests
from pathlib import Path
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
TAVILY_KEY     = os.environ.get("TAVILY_API_KEY", "")
EMAIL_TO       = os.environ.get("ALERT_EMAIL_TO",   "allndougls@gmail.com")
EMAIL_FROM     = os.environ.get("ALERT_EMAIL_FROM", "")
EMAIL_PASS     = os.environ.get("ALERT_EMAIL_PASS", "")

DATA_DIR       = Path(__file__).parent / "data" / "master"
MIN_SCORE      = 6.5
MIN_DD         = 12.0
MAX_EMPRESAS   = 8
DELAY_ENTRE_LLAMADAS = 3


def load_hot_companies():
    csvs = sorted(DATA_DIR.glob("sp500_full_export_*.csv"))
    if not csvs:
        print("  ❌ No hay CSV disponible")
        return [], {}

    latest = csvs[-1]
    print(f"  CSV: {latest.name}")

    companies = []
    with open(latest, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                score    = float(row.get('fund_score') or row.get('combined_score') or 0)
                dd       = abs(float(row.get('drawdown_60d') or 0))
                rsi      = float(row.get('rsi_14') or row.get('rsi') or 50)
                earnings = int(row.get('earnings_days_next') or 999)
                if score >= MIN_SCORE and dd >= MIN_DD and rsi < 40 and earnings > 7:
                    companies.append(row)
            except (ValueError, TypeError):
                continue

    companies.sort(key=lambda r: float(r.get('fund_score') or r.get('combined_score') or 0), reverse=True)

    market = {}
    ctx_path = DATA_DIR / "market_context.json"
    if ctx_path.exists():
        with open(ctx_path, 'r', encoding='utf-8') as f:
            market = json.load(f)

    print(f"  HOTs encontrados: {len(companies)} → analizando top {min(len(companies), MAX_EMPRESAS)}")
    return companies[:MAX_EMPRESAS], market


def search_news_tavily(ticker, company_name, sector):
    if not TAVILY_KEY:
        return None
    queries = [
        f"{ticker} {company_name} stock news earnings 2025 2026",
        f"{ticker} analyst rating downgrade upgrade outlook",
        f"{company_name} {sector} sector news recent",
    ]
    all_results = []
    for query in queries:
        try:
            response = requests.post(
                "https://api.tavily.com/search",
                headers={"Content-Type": "application/json"},
                json={
                    "api_key": TAVILY_KEY, "query": query, "search_depth": "basic",
                    "include_answer": True, "include_raw_content": False,
                    "max_results": 3,
                    "include_domains": ["reuters.com","bloomberg.com","wsj.com","cnbc.com",
                                        "marketwatch.com","seekingalpha.com","finance.yahoo.com","barrons.com"],
                }, timeout=15,
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("answer"):
                    all_results.append(f"RESUMEN: {data['answer']}")
                for r in data.get("results", [])[:2]:
                    title, content, date = r.get("title",""), r.get("content","")[:300], r.get("published_date","")
                    if title:
                        all_results.append(f"• [{date[:10] if date else 'reciente'}] {title}: {content}")
            time.sleep(1)
        except Exception as e:
            print(f"    Tavily error: {e}")
            continue
    if not all_results:
        return None
    combined = "\n".join(all_results)
    return combined[:2000] + "..." if len(combined) > 2000 else combined


def build_news_context(ticker, company_name, sector):
    print(f"    Buscando noticias con Tavily ({ticker})...")
    news = search_news_tavily(ticker, company_name, sector)
    if not news:
        return ""
    return f"""

════════════════════════════════════════════════
SIDI NEWS — CONTEXTO NOTICIOSO EN TIEMPO REAL
Fuente: búsqueda web (últimos 30-90 días)
════════════════════════════════════════════════
{news}

INSTRUCCIONES ADICIONALES:
Con la información anterior, evalúa el tipo de caída y aplica scoring.
Añade al final estas líneas exactas:
NOTICIAS_VEREDICTO: [FAVORABLE / NEUTRO / DESFAVORABLE]
NOTICIAS_SCORE: [X.X]/10
NOTICIAS_TIPO_CAIDA: [tipo]
NOTICIAS_RAZON: [1 línea con la razón principal]"""


def build_claude_report(company, market):
    r = company
    now = datetime.now().strftime('%Y-%m-%d')

    def sf(v, d=2):
        try: return round(float(v or 0), d)
        except: return 0

    def pct(v, field=''):
        n = sf(v)
        if field in ('revenue_growth','eps_growth','roe') and abs(n) <= 2:
            return round(n * 100, 1)
        if field == 'fcf_yield' and abs(n) <= 1:
            return round(n * 100, 1)
        return n

    ticker = r.get('ticker','?')
    name   = r.get('name', ticker)
    sector = r.get('sector','?')
    price  = sf(r.get('price'), 2)
    score  = sf(r.get('fund_score') or r.get('combined_score'))
    dd     = abs(sf(r.get('drawdown_60d'), 1))
    rsi    = sf(r.get('rsi_14') or r.get('rsi'), 1)
    atr    = sf(r.get('atr'), 2)

    target_pct = (1.5 * atr / price * 100) if atr > 0 and price > 0 else 6.5
    stop_price  = round(price * 0.95, 2)
    target_price= round(price * (1 + target_pct/100), 2)
    earnings_d  = r.get('earnings_days_next', 'N/D')

    mkt_regime = market.get('market_regime', 'N/D')
    vix        = market.get('vix', 0)
    combined_rec = market.get('combined_rec', '')

    report = f"""STOCK-RADAR · INFORME CUANTITATIVO
Generado: {now}
Estrategia: Mean Reversion S&P500 · Horizonte: 8-15d
Sistema validado: WF 3/3 positivas, TEST +23.9%

CONTEXTO DE MERCADO
{{
  "regime": "{mkt_regime}",
  "vix": {vix},
  "recomendacion": "{combined_rec}"
}}

EMPRESA: {ticker} · {name} · {sector}
{{
  "score_combinado": {score},
  "crecimiento": {sf(r.get('fund_growth'))},
  "solidez": {sf(r.get('fund_solidity'))},
  "valoracion": {sf(r.get('fund_valuation'))},
  "score_tecnico": {sf(r.get('tech_score'))},
  "precio": {price},
  "drawdown_60d": "-{dd}%",
  "rsi_14": {rsi},
  "macd_mejorando": {str(r.get('macd_improving','False')).strip()},
  "golden_cross": {str(r.get('golden_cross','False')).strip()},
  "tendencia": "{r.get('trend_bias','N/D')}",
  "soporte_cercano": {str(r.get('near_support','False')).strip()},
  "pe": {sf(r.get('pe'), 1)},
  "roe": "{pct(r.get('roe'),'roe')}%",
  "debt_equity": {sf(r.get('debt_equity'), 1)},
  "revenue_growth": "{pct(r.get('revenue_growth'),'revenue_growth')}%",
  "eps_growth": "{pct(r.get('eps_growth'),'eps_growth')}%",
  "fcf_ni_ratio": {sf(r.get('fcf_ni_ratio'), 2)},
  "fcf_yield": "{pct(r.get('fcf_yield_calc') or r.get('fcf_yield'), 'fcf_yield')}%",
  "stop_sugerido": "${stop_price} (-5%)",
  "target_sugerido": "${target_price} (+{round(target_pct,1)}%)",
  "earnings_proximos": "{earnings_d}d"
}}

INSTRUCCIONES:
Analiza esta empresa con la estrategia mean reversion S&P500.
Responde EXACTAMENTE en este formato:

VEREDICTO: [EXECUTE / WATCH / SKIP]
CONVICCION: [ALTA / MEDIA / BAJA]
DIAGNOSTICO_FUNDAMENTAL: [2-3 lineas sobre calidad real del negocio]
DIAGNOSTICO_TECNICO: [2-3 lineas sobre señales tecnicas]
STOP: $[precio] ([pct]%)
TARGET: $[precio] ([pct]%)
RR: [ratio]:1
RIESGOS: [max 3 factores que invalidan la tesis]
RAZON: [1 linea de justificacion del veredicto]"""

    return report


def call_claude(report_text):
    if not ANTHROPIC_KEY:
        print("  ⚠ Sin ANTHROPIC_API_KEY — saltando análisis IA")
        return None
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514", "max_tokens": 1000,
                "system": (
                    "Eres el analista cuantitativo del sistema STOCK-RADAR especializado en "
                    "swing trading de mean reversion sobre el S&P500. Cuando recibas un informe "
                    "con 'STOCK-RADAR · INFORME CUANTITATIVO' analiza y responde EXACTAMENTE "
                    "en el formato especificado. Sé directo y específico con los números. "
                    "No añadas texto adicional fuera del formato."
                ),
                "messages": [{"role": "user", "content": report_text}],
            },
            timeout=30,
        )
        if response.status_code == 200:
            return response.json()["content"][0]["text"]
        print(f"  ❌ Claude API error {response.status_code}: {response.text[:100]}")
        return None
    except Exception as e:
        print(f"  ❌ Claude error: {e}")
        return None


def parse_verdict(claude_response):
    if not claude_response:
        return "UNKNOWN", "BAJA", "NEUTRO", 5.0, "desconocida", ""

    lines = claude_response.upper().split("\n")
    veredicto, conviccion = "UNKNOWN", "BAJA"
    noticias_vered, noticias_score, noticias_tipo = "NEUTRO", 5.0, "desconocida"

    for line in lines:
        line = line.strip()
        if line.startswith("VEREDICTO:"):
            text = line.replace("VEREDICTO:", "").strip()
            if "EXECUTE" in text: veredicto = "EXECUTE"
            elif "WATCH" in text: veredicto = "WATCH"
            elif "SKIP" in text: veredicto = "SKIP"
        elif line.startswith("CONVICCION:"):
            text = line.replace("CONVICCION:", "").strip()
            if "ALTA" in text: conviccion = "ALTA"
            elif "MEDIA" in text: conviccion = "MEDIA"
            elif "BAJA" in text: conviccion = "BAJA"
        elif line.startswith("NOTICIAS_VEREDICTO:"):
            text = line.replace("NOTICIAS_VEREDICTO:", "").strip()
            if "FAVORABLE" in text: noticias_vered = "FAVORABLE"
            elif "DESFAVORABLE" in text: noticias_vered = "DESFAVORABLE"
            else: noticias_vered = "NEUTRO"
        elif line.startswith("NOTICIAS_SCORE:"):
            try: noticias_score = float(line.replace("NOTICIAS_SCORE:", "").strip().split("/")[0].strip())
            except Exception: pass
        elif line.startswith("NOTICIAS_TIPO_CAIDA:"):
            noticias_tipo = line.replace("NOTICIAS_TIPO_CAIDA:", "").strip().lower()

    return veredicto, conviccion, noticias_vered, noticias_score, noticias_tipo, claude_response


def send_email(subject, html_body, plain_body=""):
    if not EMAIL_FROM or not EMAIL_PASS:
        print(f"  ⚠ Sin credenciales email — imprimiendo en consola:\n  {subject}")
        return False
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f"SIDI STOCKS <{EMAIL_FROM}>"
        msg['To']      = EMAIL_TO
        if plain_body:
            msg.attach(MIMEText(plain_body, 'plain', 'utf-8'))
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as server:
            server.login(EMAIL_FROM, EMAIL_PASS)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"  ✅ Email enviado a {EMAIL_TO}")
        return True
    except Exception as e:
        print(f"  ❌ Email error: {e}")
        return False


def format_execute_email(ticker, company, claude_response, market):
    r = company
    def sf(v, d=2):
        try: return round(float(v or 0), d)
        except: return 0
    price  = sf(r.get('price'), 2)
    score  = sf(r.get('fund_score') or r.get('combined_score'))
    dd     = abs(sf(r.get('drawdown_60d'), 1))
    rsi    = sf(r.get('rsi_14') or r.get('rsi'), 1)
    sector = r.get('sector','?')
    name   = r.get('name', ticker)
    atr    = sf(r.get('atr'), 2)
    target_pct = (1.5 * atr / price * 100) if atr > 0 and price > 0 else 6.5
    stop_price  = round(price * 0.95, 2)
    target_price= round(price * (1 + target_pct/100), 2)
    sizing = round(200 / (price - stop_price)) if (price - stop_price) > 0 else 0
    mkt = market.get('market_regime','?')
    vix = market.get('vix', 0)
    now = datetime.now().strftime('%d/%m/%Y %H:%M')

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
  body{{font-family:'Segoe UI',Arial,sans-serif;background:#07090f;color:#e2e8f0;margin:0;padding:0}}
  .container{{max-width:600px;margin:0 auto;padding:20px}}
  .header{{background:linear-gradient(135deg,#0D1B2A,#1a2a3a);border-radius:10px;padding:24px;margin-bottom:16px;border:1px solid #1e3a5f}}
  .logo{{font-size:22px;font-weight:900;color:#fff;letter-spacing:3px}}
  .badge{{display:inline-block;background:#22c55e;color:#000;font-weight:700;font-size:14px;padding:4px 14px;border-radius:4px;letter-spacing:2px;margin-top:8px}}
  .card{{background:#0d1017;border:1px solid #1a1e2c;border-radius:8px;padding:16px;margin-bottom:12px}}
  .card.execute{{border-left:4px solid #22c55e}}
  .label{{font-size:9px;color:#64748b;letter-spacing:2px;text-transform:uppercase;margin-bottom:4px}}
  .value{{font-size:18px;font-weight:700;color:#fff}}
  .grid3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:12px}}
  .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}}
  .analysis{{background:#0a0f1a;border:1px solid #1e3a5f;border-radius:6px;padding:14px;font-size:12px;line-height:1.7;color:#94a3b8;font-family:monospace;white-space:pre-wrap}}
  .footer{{font-size:10px;color:#475569;text-align:center;margin-top:20px}}
  .sizing{{background:rgba(14,165,233,0.08);border:1px solid rgba(14,165,233,0.2);border-radius:6px;padding:12px;margin-top:12px}}
</style></head><body><div class="container">
  <div class="header">
    <div class="logo">⚔ SIDI·STOCKS</div>
    <div class="badge">✅ EXECUTE</div>
    <div style="margin-top:10px;font-size:13px;color:#94a3b8">{now} · {mkt} · VIX {vix:.1f}</div>
  </div>
  <div class="card execute">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px">
      <div>
        <div style="font-size:28px;font-weight:900;color:#fff">{ticker}</div>
        <div style="font-size:13px;color:#64748b">{name}</div>
        <div style="font-size:11px;color:#475569;margin-top:2px">{sector}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:24px;font-weight:700;color:#fff">${price}</div>
        <div style="font-size:12px;color:#ef4444">-{dd}% en 60d</div>
      </div>
    </div>
    <div class="grid3">
      <div><div class="label">Score</div><div class="value {'green' if score >= 7 else ''}">{score}</div></div>
      <div><div class="label">RSI</div><div class="value">{rsi}</div></div>
      <div><div class="label">Drawdown</div><div class="value" style="color:#ef4444">-{dd}%</div></div>
    </div>
    <div class="grid2">
      <div><div class="label">Stop Loss (-5%)</div><div class="value" style="color:#ef4444">${stop_price}</div></div>
      <div><div class="label">Target (+{round(target_pct,1)}%)</div><div class="value" style="color:#22c55e">${target_price}</div></div>
    </div>
    <div class="sizing">
      <div class="label">Sizing (10.000€ · riesgo 2%)</div>
      <div style="font-size:15px;font-weight:700;color:#0ea5e9;margin-top:4px">
        {sizing} acciones · ~{round(sizing*price):,}€ exposición · 200€ riesgo máximo
      </div>
    </div>
  </div>
  <div class="card">
    <div class="label" style="margin-bottom:10px">Análisis Claude — Veredicto completo</div>
    <div class="analysis">{claude_response}</div>
  </div>
  <div class="footer">
    SIDI STOCKS · Sistema de Mean Reversion S&P500<br>
    Walk-Forward validado · Edge confirmado 3/3 ventanas · TEST promedio +23.9%<br>
    Este análisis es informativo. No es consejo financiero.
  </div>
</div></body></html>"""

    plain = f"""SIDI STOCKS · EXECUTE ✅
{now}

{ticker} — {name} ({sector})
Precio: ${price} | Score: {score} | RSI: {rsi} | DD: -{dd}%
Stop: ${stop_price} | Target: ${target_price} (+{round(target_pct,1)}%)
Sizing: {sizing} acciones (~{round(sizing*price):,}€)

--- ANÁLISIS CLAUDE ---
{claude_response}

---
SIDI STOCKS · Mean Reversion S&P500"""

    return html, plain


def format_summary_email(resultados, market, n_hot):
    now = datetime.now().strftime('%d/%m/%Y %H:%M')
    mkt = market.get('market_regime','?')
    vix = market.get('vix', 0)
    executes = [r for r in resultados if r['veredicto'] == 'EXECUTE']
    watches  = [r for r in resultados if r['veredicto'] == 'WATCH']
    skips    = [r for r in resultados if r['veredicto'] == 'SKIP']

    rows = ""
    for r in resultados:
        color = '#22c55e' if r['veredicto']=='EXECUTE' else '#f59e0b' if r['veredicto']=='WATCH' else '#ef4444'
        ncolor = '#22c55e' if r.get('noticias')=='FAVORABLE' else '#f59e0b' if r.get('noticias')=='NEUTRO' else '#ef4444'
        rows += f"""
        <tr style="border-bottom:1px solid #1a1e2c">
          <td style="padding:8px;font-weight:700;color:#fff">{r['ticker']}</td>
          <td style="padding:8px;color:#94a3b8">{r['name'][:20]}</td>
          <td style="padding:8px;text-align:center"><span style="color:{color};font-weight:700">{r['veredicto']}</span></td>
          <td style="padding:8px;text-align:center"><span style="color:{ncolor}">{r.get('noticias','N/A')}</span>{f" ({r.get('noticias_score',0):.1f})" if r.get('noticias_score') else ""}</td>
          <td style="padding:8px;text-align:right;color:#fff">{r['score']}</td>
          <td style="padding:8px;text-align:right;color:#ef4444">-{r['dd']}%</td>
        </tr>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
  body{{font-family:'Segoe UI',Arial,sans-serif;background:#07090f;color:#e2e8f0;margin:0;padding:20px}}
  .container{{max-width:600px;margin:0 auto}}
  table{{width:100%;border-collapse:collapse;background:#0d1017;border-radius:8px;overflow:hidden}}
  th{{background:#0D1B2A;padding:10px 8px;font-size:10px;color:#64748b;letter-spacing:2px;text-transform:uppercase;text-align:left}}
</style></head><body><div class="container">
  <div style="background:linear-gradient(135deg,#0D1B2A,#1a2a3a);border-radius:10px;padding:20px;margin-bottom:16px;border:1px solid #1e3a5f">
    <div style="font-size:20px;font-weight:900;color:#fff;letter-spacing:3px">⚔ SIDI·STOCKS</div>
    <div style="font-size:13px;color:#94a3b8;margin-top:6px">Resumen diario · {now}</div>
    <div style="font-size:12px;color:#64748b">{mkt} · VIX {vix:.1f} · {n_hot} HOTs detectados · {len(resultados)} analizados</div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:16px">
    <div style="background:#0d1017;border:1px solid rgba(34,197,94,0.2);border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:28px;font-weight:900;color:#22c55e">{len(executes)}</div>
      <div style="font-size:10px;color:#64748b;letter-spacing:2px">EXECUTE</div>
    </div>
    <div style="background:#0d1017;border:1px solid rgba(245,158,11,0.2);border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:28px;font-weight:900;color:#f59e0b">{len(watches)}</div>
      <div style="font-size:10px;color:#64748b;letter-spacing:2px">WATCH</div>
    </div>
    <div style="background:#0d1017;border:1px solid rgba(239,68,68,0.2);border-radius:8px;padding:14px;text-align:center">
      <div style="font-size:28px;font-weight:900;color:#ef4444">{len(skips)}</div>
      <div style="font-size:10px;color:#64748b;letter-spacing:2px">SKIP</div>
    </div>
  </div>
  <table>
    <tr><th>Ticker</th><th>Empresa</th><th>Veredicto</th><th>Noticias</th><th>Score</th><th>DD 60d</th></tr>
    {rows}
  </table>
  <div style="font-size:10px;color:#475569;text-align:center;margin-top:20px">
    SIDI STOCKS · Mean Reversion S&P500 · No es consejo financiero
  </div>
</div></body></html>"""

    executes_str = ', '.join(r['ticker'] for r in executes) if executes else 'Ninguno'
    plain = f"""SIDI STOCKS · Resumen diario {now}
{mkt} · VIX {vix:.1f}

HOTs detectados: {n_hot}
Analizados: {len(resultados)}
EXECUTE: {len(executes)} ({executes_str})
WATCH: {len(watches)}
SKIP: {len(skips)}
"""
    for r in resultados:
        plain += f"  {r['ticker']:<6} {r['veredicto']:<8} Score={r['score']} DD=-{r['dd']}%\n"

    return html, plain


def run_agent():
    print(f"\n{'='*60}")
    print(f"  SIDI STOCKS · Agente Claude — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    hot_companies, market = load_hot_companies()
    if not hot_companies:
        print("  Sin HOTs hoy — no hay alertas")
        return []

    resultados = []
    for i, company in enumerate(hot_companies):
        ticker = company.get('ticker','?')
        score  = round(float(company.get('fund_score') or company.get('combined_score') or 0), 2)
        dd     = abs(round(float(company.get('drawdown_60d') or 0), 1))
        rsi    = round(float(company.get('rsi_14') or company.get('rsi') or 50), 1)

        print(f"\n  [{i+1}/{len(hot_companies)}] {ticker} — Score {score} DD -{dd}% RSI {rsi}")

        news_context = build_news_context(ticker, company.get("name", ticker), company.get("sector","?")) if TAVILY_KEY else ""
        report = build_claude_report(company, market)
        if news_context:
            report += news_context

        print(f"    Analizando con Claude{' + noticias' if news_context else ''}...")
        claude_resp = call_claude(report)
        if not claude_resp:
            continue

        veredicto, conviccion, noticias_v, noticias_sc, noticias_tipo, full_resp = parse_verdict(claude_resp)
        noticias_str = f" · Noticias: {noticias_v}" if TAVILY_KEY else ""
        print(f"    Veredicto: {veredicto} · Convicción: {conviccion}{noticias_str}")

        if veredicto == "EXECUTE" and noticias_v == "DESFAVORABLE":
            print(f"    ⚠ EXECUTE bloqueado por noticias DESFAVORABLE → degradado a WATCH")
            veredicto = "WATCH"

        resultados.append({
            "ticker": ticker, "name": company.get("name", ticker), "sector": company.get("sector","?"),
            "score": score, "dd": dd, "rsi": rsi, "veredicto": veredicto, "conviccion": conviccion,
            "noticias": noticias_v if TAVILY_KEY else "N/A",
            "noticias_score": noticias_sc if TAVILY_KEY else None,
            "noticias_tipo": noticias_tipo if TAVILY_KEY else "N/A",
            "analysis": full_resp,
        })

        if veredicto == 'EXECUTE':
            print(f"    ✅ EXECUTE — enviando email...")
            html, plain = format_execute_email(ticker, company, full_resp, market)
            send_email(
                subject=f"⚔ SIDI STOCKS · EXECUTE: {ticker} · Score {score} — -{dd}% DD",
                html_body=html, plain_body=plain,
            )

        if i < len(hot_companies) - 1:
            time.sleep(DELAY_ENTRE_LLAMADAS)

    if resultados:
        n_hot_total = len(hot_companies)
        executes = [r for r in resultados if r['veredicto'] == 'EXECUTE']
        subject = (
            f"⚔ SIDI STOCKS · {len(executes)} EXECUTE{'s' if len(executes)>1 else ''} hoy"
            if executes else f"⚔ SIDI STOCKS · Resumen {datetime.now().strftime('%d/%m')} — Sin señales EXECUTE"
        )
        html_s, plain_s = format_summary_email(resultados, market, n_hot_total)
        send_email(subject=subject, html_body=html_s, plain_body=plain_s)

    print(f"\n  Agente completado — {len(resultados)} empresas analizadas")
    print(f"  EXECUTEs: {sum(1 for r in resultados if r['veredicto']=='EXECUTE')}")
    print(f"{'='*60}\n")
    return resultados


if __name__ == '__main__':
    run_agent()
