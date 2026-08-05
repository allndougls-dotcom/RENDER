"""
SIDI STOCKS · Scheduler
Actualización diaria automática + agentes Claude y GPT News
"""
import os
import subprocess
from pathlib import Path
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


def run_daily_pipeline():
    print(f"\n  [{datetime.now().strftime('%H:%M')}] ═══ Iniciando pipeline diario ═══")
    base = str(Path(__file__).parent)

    print(f"  [1/4] Ingesta de datos...")
    try:
        subprocess.run(['python', 'main_ingesta.py'], cwd=base, timeout=3600, check=True)
        print(f"  [1/4] ✅ Ingesta completada")
    except Exception as e:
        print(f"  [1/4] ❌ Error en ingesta: {e}")
        return

    print(f"  [2/4] Recargando cache...")
    try:
        import requests
        port = os.environ.get('PORT', 8080)
        requests.post(f'http://localhost:{port}/refresh-cache', timeout=5)
        print(f"  [2/4] ✅ Cache actualizada")
    except Exception as e:
        print(f"  [2/4] ⚠ Cache: {e}")

    print(f"  [3/4] Ejecutando agente Claude...")
    claude_results = []
    try:
        from agente import run_agent
        claude_results = run_agent() or []
        print(f"  [3/4] ✅ Agente Claude completado")
    except Exception as e:
        print(f"  [3/4] ❌ Error agente Claude: {e}")

    print(f"  [4/4] Ejecutando SIDI News Analyzer (GPT-4o)...")
    gpt_results = []
    try:
        from agente_gpt import run_gpt_agent
        gpt_results = run_gpt_agent() or []
        print(f"  [4/4] ✅ SIDI News completado")
    except Exception as e:
        print(f"  [4/4] ❌ Error SIDI News: {e}")

    if claude_results and gpt_results:
        try:
            _send_combined_summary(claude_results, gpt_results)
        except Exception as e:
            print(f"  ⚠ Error resumen combinado: {e}")

    print(f"  [{datetime.now().strftime('%H:%M')}] ═══ Pipeline completado ═══\n")


def _send_combined_summary(claude_results, gpt_results):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    EMAIL_TO   = os.environ.get("ALERT_EMAIL_TO",  "allndougls@gmail.com")
    EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM","")
    EMAIL_PASS = os.environ.get("ALERT_EMAIL_PASS","")
    if not EMAIL_FROM or not EMAIL_PASS:
        return

    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    gpt_map = {r["ticker"]: r for r in gpt_results}
    rows, alta_conviccion = "", []

    for cr in claude_results:
        ticker = cr["ticker"]
        gr = gpt_map.get(ticker, {})
        c_verd, g_verd = cr.get("veredicto","—"), gr.get("veredicto","N/A")
        g_score = gr.get("news_score", 0)
        is_top = c_verd == "EXECUTE" and g_verd == "FAVORABLE"
        if is_top: alta_conviccion.append(ticker)

        c_color = "#22c55e" if c_verd=="EXECUTE" else "#f59e0b" if c_verd=="WATCH" else "#ef4444"
        g_color = "#22c55e" if g_verd=="FAVORABLE" else "#f59e0b" if g_verd=="NEUTRO" else "#ef4444"
        score_str = f"{g_score:.1f}" if g_score else "—"

        rows += f"""<tr style="border-bottom:1px solid #1a1e2c;{'background:rgba(34,197,94,0.05)' if is_top else ''}">
          <td style="padding:7px 10px;font-weight:700;color:#fff">{ticker} {'⭐' if is_top else ''}</td>
          <td style="padding:7px 10px;color:#94a3b8;font-size:10px">{cr.get('name','')[:16]}</td>
          <td style="padding:7px 10px;text-align:center;color:{c_color};font-weight:700">{c_verd}</td>
          <td style="padding:7px 10px;text-align:center;color:{g_color};font-weight:700">{g_verd}</td>
          <td style="padding:7px 10px;text-align:center;color:#fff">{score_str}</td>
          <td style="padding:7px 10px;text-align:center;color:#94a3b8">{cr.get('score',0)}</td>
          <td style="padding:7px 10px;text-align:center;color:#ef4444">-{cr.get('dd',0)}%</td>
        </tr>"""

    top_str = ", ".join(alta_conviccion) if alta_conviccion else "Ninguna"
    top_banner = (f'<div style="background:rgba(34,197,94,0.08);border:1px solid rgba(34,197,94,0.3);'
                  f'border-radius:8px;padding:14px;margin-bottom:14px">'
                  f'<div style="font-weight:700;color:#22c55e;margin-bottom:6px">⭐ ALTA CONVICCIÓN — Ambos agentes de acuerdo</div>'
                  f'<div style="color:#94a3b8;font-size:11px">Claude EXECUTE + GPT FAVORABLE: {top_str}</div></div>'
                  if alta_conviccion else '')

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{font-family:'Segoe UI',Arial,sans-serif;background:#07090f;color:#e2e8f0;padding:20px}}
.c{{max-width:640px;margin:0 auto}} table{{width:100%;border-collapse:collapse;background:#0d1017;border-radius:8px}}
th{{background:#0D1B2A;padding:9px 10px;font-size:9px;color:#64748b;letter-spacing:2px;text-transform:uppercase;text-align:left}}</style></head>
<body><div class="c">
  <div style="background:linear-gradient(135deg,#0D1B2A,#1a2a3a);border-radius:10px;padding:20px;margin-bottom:14px;border:1px solid #1e3a5f">
    <div style="font-size:20px;font-weight:900;color:#fff;letter-spacing:3px">⚔ SIDI·STOCKS</div>
    <div style="font-size:12px;color:#94a3b8;margin-top:6px">Análisis Dual · Claude + GPT · {now}</div>
  </div>
  {top_banner}
  <table>
    <tr><th>Ticker</th><th>Empresa</th><th>Claude</th><th>GPT News</th><th>News Score</th><th>Score</th><th>DD 60d</th></tr>
    {rows}
  </table>
  <div style="font-size:10px;color:#475569;text-align:center;margin-top:16px">SIDI STOCKS · Análisis Dual · No es consejo financiero</div>
</div></body></html>"""

    n_exec = sum(1 for r in claude_results if r.get("veredicto")=="EXECUTE")
    subj = (f"⭐ SIDI STOCKS · Alta convicción: {', '.join(alta_conviccion)} · {now.split()[0]}"
            if alta_conviccion else f"⚔ SIDI STOCKS · Análisis Dual · {n_exec} EXECUTE · {now.split()[0]}")

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subj, f"SIDI STOCKS <{EMAIL_FROM}>", EMAIL_TO
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as s:
            s.login(EMAIL_FROM, EMAIL_PASS)
            s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"  ✅ Resumen combinado enviado · Alta convicción: {len(alta_conviccion)}")
    except Exception as e:
        print(f"  ❌ Error resumen combinado: {e}")


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_daily_pipeline,
        trigger=CronTrigger(hour=8, minute=0),
        id='daily_pipeline',
        name='Pipeline diario SIDI STOCKS',
        replace_existing=True,
    )
    scheduler.start()
    print(f"  Scheduler activo — pipeline diario a las 08:00 UTC")
    print(f"  Flujo: ingesta → cache → agente Claude → agente GPT News → resumen combinado")
    return scheduler
