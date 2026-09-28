from pathlib import Path
import re


def sub1(text, pattern, repl, label, flags=0):
    out, n = re.subn(pattern, repl, text, count=1, flags=flags)
    if n != 1:
        raise RuntimeError(f"{label}: expected 1 match, got {n}")
    return out


p = Path("stock-radar-v3.html")
html = p.read_text(encoding="utf-8")

# Scanner defaults.
html = sub1(html, r'(<input type="range" min="0" max="10" step="0\.5" )value="6"( id="fScore")', r'\1value="6.5"\2', "score slider")
html = html.replace('<span id="fScoreVal">6.0</span>', '<span id="fScoreVal">6.5</span>', 1)
html = sub1(html, r'(<input type="range" min="0" max="40" step="1" )value="8"( id="fDD")', r'\1value="12"\2', "DD slider")
html = html.replace('<span id="fDDVal">8%</span>', '<span id="fDDVal">12%</span>', 1)
html = html.replace("document.getElementById('fScore').value = 6;", "document.getElementById('fScore').value = 6.5;", 1)
html = html.replace("document.getElementById('fDD').value = 8;", "document.getElementById('fDD').value = 12;", 1)

# CSV mapper: inject V1 fields immediately after setupHot.
pattern = r"(?m)^(\s*)setupHot:\s*String\(r\.setup_hot\) === 'True' \|\| r\.setup_hot === true,\s*$"
m = re.search(pattern, html)
if not m:
    raise RuntimeError("CSV mapper setupHot line not found")
indent = m.group(1)
block = f'''{indent}setupHot:          String(r.setup_hot).toLowerCase() === 'true' || r.setup_hot === true,
{indent}fullSetup:         String(r.sidi_full_setup ?? r.full_setup).toLowerCase() === 'true' || r.sidi_full_setup === true || r.full_setup === true,
{indent}strategyVersion:   r.sidi_strategy_version || '',
{indent}contextReady:      String(r.sidi_context_ready).toLowerCase() === 'true' || r.sidi_context_ready === true,
{indent}spy20:             parseFloat(r.spy_return_20d || 0),
{indent}abnormal20:        parseFloat(r.abnormal_return_20d || 0),
{indent}sidiGateFailures:  r.sidi_gate_failures || '',
{indent}dayOpen:           parseFloat(r.day_open || 0),
{indent}dayHigh:           parseFloat(r.day_high || 0),
{indent}dayLow:            parseFloat(r.day_low || 0),
{indent}dayClose:          parseFloat(r.day_close || r.price || 0),
{indent}atrSignal:         parseFloat(r.sidi_atr14_signal || r.atr_14 || 0),
{indent}sidiEntryRule:     r.sidi_entry_rule || '',
{indent}sidiTpAtrMult:     parseFloat(r.sidi_tp_atr_mult || 0.75),
{indent}sidiSlPct:         parseFloat(r.sidi_sl_pct || -5),
{indent}sidiTimeStop:      parseInt(r.sidi_time_stop_sessions || 7),
{indent}sidiRiskPct:       parseFloat(r.sidi_risk_pct || 1.5),
{indent}sidiMaxPositions:  parseInt(r.sidi_max_positions || 5),'''
html = re.sub(pattern, lambda _: block, html, count=1)

# Scanner badges and stats.
html = sub1(html,
    r"\s*const isHot = d\.setupHot === true;\n\s*const isWatch = !isHot && d\.drawdown60 >= 8 && d\.score >= 6;\n\s*const setupLabel = isHot \? '<span class=\"setup-badge setup-hot\">HOT</span>' : isWatch \? '<span class=\"setup-badge setup-watch\">WATCH</span>' : '<span class=\"setup-badge setup-none\">—</span>';",
    '''\n    const isFull = d.fullSetup === true;\n    const isHot = d.setupHot === true;\n    const isWatch = !isHot && d.drawdown60 >= 12 && d.score >= 6.5;\n    const setupLabel = isFull ? '<span class="setup-badge setup-hot" title="SIDI_SHADOW_V1: Fund≥6.5 · DD60≥12 · RSI<40 · MACD↑ · Vol↓ · SPY20≤+1 · Abnormal20≤-10">FULL V1</span>' : isHot ? '<span class="setup-badge setup-watch">HOT TÉCNICO</span>' : isWatch ? '<span class="setup-badge setup-watch">WATCH</span>' : '<span class="setup-badge setup-none">—</span>';''',
    "scanner badges")
html = html.replace("  const hot = filteredData.filter(d => d.setupHot === true);", "  const full = filteredData.filter(d => d.fullSetup === true);\n  const hot = filteredData.filter(d => d.setupHot === true);", 1)
html = html.replace('<div class="stat-card amber"><div class="stat-label">Setups HOT</div><div class="stat-val" style="color:var(--amber)">${hot.length}</div><div class="stat-sub">técnico + score + drawdown</div></div>', '<div class="stat-card amber"><div class="stat-label">FULL SIDI V1</div><div class="stat-val" style="color:var(--amber)">${full.length}</div><div class="stat-sub">7 puertas congeladas</div></div>', 1)

# Risk matrix / execution rules.
html = sub1(html,
    r"const stopDist = \(res\.atrNow \* 2 / res\.price \* 100\)\.toFixed\(1\);\s*\n\s*const targetDist = \(res\.atrNow \* 3 / res\.price \* 100\)\.toFixed\(1\);\s*\n\s*const rr = \(parseFloat\(targetDist\) / parseFloat\(stopDist\)\)\.toFixed\(2\);",
    "const stopDist = 5.0;\n  const targetDist = (res.atrNow * 0.75 / res.price * 100).toFixed(1);\n  const rr = (parseFloat(targetDist) / stopDist).toFixed(2);",
    "risk calculations")
html = html.replace('Stop Loss (2×ATR)', 'SL SIDI V1 (-5%)', 1)
html = html.replace('Target (3×ATR)', 'TP SIDI V1 (0.75×ATR)', 1)
html = html.replace('El target primario es 3×ATR desde entrada. El target secundario es la resistencia más cercana. Sale primero quien llegue.', 'Señal al cierre T → entrada Open T+1 · TP = 0.75×ATR14 de señal · SL = -5% · time-stop = 7 sesiones · gap bajo SL = salida al Open · si TP y SL se tocan en la misma vela, SL primero.', 1)

# Registro legacy: freeze it before Shadow tracker is installed, so it cannot
# pollute new forward observations with the old close/+6.5%/15d engine.
html = html.replace('Seguimiento automático de resultado (WIN/LOSS) y Win Rate por warning', 'LEGACY · sólo histórico hasta activar Shadow V1', 1)
html = sub1(html,
    r'Cada vez que cargas un CSV nuevo:.*?si pasaron 15 días sin resolver\.',
    '<strong style="color:var(--amber)">Registro legacy en modo sólo lectura.</strong> La estrategia activa es <strong style="color:var(--accent)">SIDI_SHADOW_V1</strong>. No se añadirán observaciones nuevas con el motor antiguo (+6.5%/15 días/entrada al cierre). El nuevo tracker usará Open T+1, TP 0.75×ATR, SL -5% y 7 sesiones.',
    'registro description', flags=re.S)
needle = 'function registrarSeñalesAutomaticas(filename) {'
if html.count(needle) != 1:
    raise RuntimeError(f"registro function expected once, got {html.count(needle)}")
html = html.replace(needle, needle + "\n  if (allData.some(d => d.strategyVersion === 'SIDI_SHADOW_V1')) { renderRegistro(); return; }", 1)

p.write_text(html, encoding="utf-8")

# API payload: make plan shown by server match frozen V1.
p = Path("servidor_local.py")
server = p.read_text(encoding="utf-8")
server = sub1(server,
    r"    stop_loss = round\(price \* 0\.95, 4\) if price is not None else None\n    tp1 = round\(price \+ atr, 4\) if price is not None and atr is not None else None\n    tp2 = round\(price \+ 1\.5 \* atr, 4\) if price is not None and atr is not None else None",
    "    stop_loss = round(price * 0.95, 4) if price is not None else None\n    tp_v1 = round(price + 0.75 * atr, 4) if price is not None and atr is not None else None",
    "server calculations")
server = server.replace('            "full_setup": _bool(row.get("full_setup")),\n            "combined_score": _float(row.get("combined_score")),', '            "full_setup": _bool(row.get("full_setup")),\n            "strategy_version": row.get("sidi_strategy_version") or "SIDI_SHADOW_V1",\n            "gate_failures": row.get("sidi_gate_failures") or None,\n            "combined_score": _float(row.get("combined_score")),', 1)
server = server.replace('            "vix": _float(row.get("vix")),\n        },', '            "vix": _float(row.get("vix")),\n            "spy_return_20d_pct": _float(row.get("spy_return_20d")),\n            "abnormal_return_20d_pct": _float(row.get("abnormal_return_20d")),\n            "sidi_context_ready": _bool(row.get("sidi_context_ready")),\n        },', 1)
server = sub1(server,
    r'''        "risk_plan": \{\n            "stop_loss_pct": -5\.0,\n            "stop_loss": stop_loss,\n            "target_tp1_1x_atr": tp1,\n            "target_tp2_1_5x_atr": tp2,\n        \},''',
    '''        "risk_plan": {\n            "strategy_version": row.get("sidi_strategy_version") or "SIDI_SHADOW_V1",\n            "entry_rule": row.get("sidi_entry_rule") or "NEXT_SESSION_OPEN",\n            "entry_status": row.get("sidi_entry_status") or "PENDING_NEXT_OPEN",\n            "atr14_signal": _float(row.get("sidi_atr14_signal") or row.get("atr_14")),\n            "target_atr_multiple": _float(row.get("sidi_tp_atr_mult"), 0.75),\n            "stop_loss_pct": _float(row.get("sidi_sl_pct"), -5.0),\n            "time_stop_sessions": _int(row.get("sidi_time_stop_sessions"), 7),\n            "risk_pct": _float(row.get("sidi_risk_pct"), 1.5),\n            "max_positions": _int(row.get("sidi_max_positions"), 5),\n            "gap_stop_rule": row.get("sidi_gap_stop_rule") or "EXIT_AT_OPEN_IF_OPEN_BELOW_SL",\n            "intraday_conflict_rule": row.get("sidi_intraday_conflict_rule") or "SL_FIRST",\n            "stop_loss_indicative_from_signal_close": stop_loss,\n            "target_indicative_from_signal_close": tp_v1,\n            "note": "Exact TP/SL are fixed from actual Open T+1",\n        },''',
    "server risk plan")
p.write_text(server, encoding="utf-8")
print("SIDI_SHADOW_V1 UI/API migration v2 applied")
