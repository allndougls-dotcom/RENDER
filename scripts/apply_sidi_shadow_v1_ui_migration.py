from pathlib import Path


def replace_once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        raise RuntimeError(f"{label}: expected 1 match, got {n}")
    return text.replace(old, new, 1)


# ── HTML / Scanner ────────────────────────────────────────────────────────────
p = Path("stock-radar-v3.html")
html = p.read_text(encoding="utf-8")

html = replace_once(html,
'''      <input type="range" min="0" max="10" step="0.5" value="6" id="fScore" oninput="updateFilters()">
      <span id="fScoreVal">6.0</span>''',
'''      <input type="range" min="0" max="10" step="0.5" value="6.5" id="fScore" oninput="updateFilters()">
      <span id="fScoreVal">6.5</span>''', "scanner score default")

html = replace_once(html,
'''      <input type="range" min="0" max="40" step="1" value="8" id="fDD" oninput="updateFilters()">
      <span id="fDDVal">8%</span>''',
'''      <input type="range" min="0" max="40" step="1" value="12" id="fDD" oninput="updateFilters()">
      <span id="fDDVal">12%</span>''', "scanner DD default")

html = replace_once(html,
'''  document.getElementById('fScore').value = 6;
  document.getElementById('fDD').value = 8;''',
'''  document.getElementById('fScore').value = 6.5;
  document.getElementById('fDD').value = 12;''', "reset filters")

html = replace_once(html,
'''    setupHot:          String(r.setup_hot) === 'True' || r.setup_hot === true,
    horizon:           r.horizon || '', ''',
'''    setupHot:          String(r.setup_hot).toLowerCase() === 'true' || r.setup_hot === true,
    fullSetup:         String(r.sidi_full_setup ?? r.full_setup).toLowerCase() === 'true' || r.sidi_full_setup === true || r.full_setup === true,
    strategyVersion:   r.sidi_strategy_version || '',
    contextReady:      String(r.sidi_context_ready).toLowerCase() === 'true' || r.sidi_context_ready === true,
    spy20:             parseFloat(r.spy_return_20d || 0),
    abnormal20:        parseFloat(r.abnormal_return_20d || 0),
    sidiGateFailures:  r.sidi_gate_failures || '',
    dayOpen:           parseFloat(r.day_open || 0),
    dayHigh:           parseFloat(r.day_high || 0),
    dayLow:            parseFloat(r.day_low || 0),
    dayClose:          parseFloat(r.day_close || r.price || 0),
    atrSignal:         parseFloat(r.sidi_atr14_signal || r.atr_14 || 0),
    sidiEntryRule:     r.sidi_entry_rule || '',
    sidiTpAtrMult:     parseFloat(r.sidi_tp_atr_mult || 0.75),
    sidiSlPct:         parseFloat(r.sidi_sl_pct || -5),
    sidiTimeStop:      parseInt(r.sidi_time_stop_sessions || 7),
    sidiRiskPct:       parseFloat(r.sidi_risk_pct || 1.5),
    sidiMaxPositions:  parseInt(r.sidi_max_positions || 5),
    horizon:           r.horizon || '', ''', "CSV SIDI mapping")

html = replace_once(html,
'''    const isHot = d.setupHot === true;
    const isWatch = !isHot && d.drawdown60 >= 8 && d.score >= 6;
    const setupLabel = isHot ? '<span class="setup-badge setup-hot">HOT</span>' : isWatch ? '<span class="setup-badge setup-watch">WATCH</span>' : '<span class="setup-badge setup-none">—</span>';''',
'''    const isFull = d.fullSetup === true;
    const isHot = d.setupHot === true;
    const isWatch = !isHot && d.drawdown60 >= 12 && d.score >= 6.5;
    const setupLabel = isFull ? '<span class="setup-badge setup-hot" title="SIDI_SHADOW_V1: Fund≥6.5 · DD60≥12 · RSI<40 · MACD↑ · Vol↓ · SPY20≤+1 · Abnormal20≤-10">FULL V1</span>' : isHot ? '<span class="setup-badge setup-watch">HOT TÉCNICO</span>' : isWatch ? '<span class="setup-badge setup-watch">WATCH</span>' : '<span class="setup-badge setup-none">—</span>';''', "scanner setup badge")

html = replace_once(html,
'''  const hot = filteredData.filter(d => d.setupHot === true);
  const hotNoWarn = filteredData.filter(d => d.setupHot === true && (d.warningCount || 0) === 0);''',
'''  const full = filteredData.filter(d => d.fullSetup === true);
  const hot = filteredData.filter(d => d.setupHot === true);
  const hotNoWarn = filteredData.filter(d => d.setupHot === true && (d.warningCount || 0) === 0);''', "stats full set")

html = replace_once(html,
'''    <div class="stat-card amber"><div class="stat-label">Setups HOT</div><div class="stat-val" style="color:var(--amber)">${hot.length}</div><div class="stat-sub">técnico + score + drawdown</div></div>''',
'''    <div class="stat-card amber"><div class="stat-label">FULL SIDI V1</div><div class="stat-val" style="color:var(--amber)">${full.length}</div><div class="stat-sub">7 puertas congeladas</div></div>''', "stats full card")

# Risk matrix: close-T values are indications; exact levels are fixed from Open T+1.
html = replace_once(html,
'''  const stopDist = (res.atrNow * 2 / res.price * 100).toFixed(1);
  const targetDist = (res.atrNow * 3 / res.price * 100).toFixed(1);
  const rr = (parseFloat(targetDist) / parseFloat(stopDist)).toFixed(2);''',
'''  const stopDist = 5.0;
  const targetDist = (res.atrNow * 0.75 / res.price * 100).toFixed(1);
  const rr = (parseFloat(targetDist) / stopDist).toFixed(2);''', "risk matrix calculations")

html = replace_once(html,
'''    <div class="risk-item"><div class="risk-label">Stop Loss (2×ATR)</div><div class="risk-val" style="color:var(--red)">-${stopDist}%</div><div class="risk-sub">$${(res.price * (1 - parseFloat(stopDist)/100)).toFixed(2)}</div></div>
    <div class="risk-item"><div class="risk-label">Target (3×ATR)</div><div class="risk-val" style="color:var(--green)">+${targetDist}%</div><div class="risk-sub">$${(res.price * (1 + parseFloat(targetDist)/100)).toFixed(2)}</div></div>''',
'''    <div class="risk-item"><div class="risk-label">SL SIDI V1 (-5%)</div><div class="risk-val" style="color:var(--red)">-5.0%</div><div class="risk-sub">indicativo desde cierre; exacto desde Open T+1</div></div>
    <div class="risk-item"><div class="risk-label">TP SIDI V1 (0.75×ATR)</div><div class="risk-val" style="color:var(--green)">+${targetDist}%</div><div class="risk-sub">offset ATR señal; exacto desde Open T+1</div></div>''', "risk matrix labels")

html = html.replace(
'''      <strong style="color:var(--white)">Regla de salida:</strong> El target primario es 3×ATR desde entrada. El target secundario es la resistencia más cercana. Sale primero quien llegue.''',
'''      <strong style="color:var(--white)">SIDI_SHADOW_V1:</strong> señal al cierre T → entrada Open T+1 · TP = 0.75×ATR14 de señal · SL = -5% · time-stop = 7 sesiones · gap bajo SL = salida al Open · si TP y SL se tocan en la misma vela, SL primero.''')

# Prevent legacy Registro from contaminating Shadow V1 observations.
html = replace_once(html,
'''  <div class="section-title">Registro de Señales <small style="font-family:'DM Mono',monospace;font-size:10px;color:var(--text2);font-weight:400;letter-spacing:1px">Seguimiento automático de resultado (WIN/LOSS) y Win Rate por warning</small> <span id="registroSyncStatus" style="display:none;font-family:'DM Mono',monospace;font-size:10px;margin-left:12px"></span></div>''',
'''  <div class="section-title">Registro de Señales <small style="font-family:'DM Mono',monospace;font-size:10px;color:var(--amber);font-weight:500;letter-spacing:1px">LEGACY · sólo histórico hasta activar Shadow V1</small> <span id="registroSyncStatus" style="display:none;font-family:'DM Mono',monospace;font-size:10px;margin-left:12px"></span></div>''', "registro legacy title")

old_desc = '''    Cada vez que cargas un CSV nuevo: <strong style="color:var(--accent)">(1)</strong> se registran las empresas que cumplen mínimos (Score≥6.5, DD≥8%), <strong style="color:var(--accent)">con o sin warning</strong> · <strong style="color:var(--accent)">(2)</strong> las señales pendientes se evalúan contra el precio actual — <strong style="color:var(--green)">WIN</strong> si tocó +6.5%, <strong style="color:var(--red)">LOSS</strong> si tocó -5%, <strong style="color:var(--text3)">EXPIRED</strong> si pasaron 15 días sin resolver.'''
new_desc = '''    <strong style="color:var(--amber)">Registro legacy en modo sólo lectura.</strong> La estrategia activa es <strong style="color:var(--accent)">SIDI_SHADOW_V1</strong>. No se añadirán observaciones nuevas con el motor antiguo (+6.5%/15 días/entrada al cierre). El nuevo tracker usará Open T+1, TP 0.75×ATR, SL -5% y 7 sesiones.'''
html = replace_once(html, old_desc, new_desc, "registro legacy description")

# Insert an early guard in the old auto-register function.
needle = '''function registrarSeñalesAutomaticas(filename) {'''
if html.count(needle) != 1:
    raise RuntimeError(f"registro function: expected 1 match, got {html.count(needle)}")
html = html.replace(needle, needle + '''
  // SIDI_SHADOW_V1 must not be evaluated with the legacy close-entry/+6.5%/15d engine.
  if (allData.some(d => d.strategyVersion === 'SIDI_SHADOW_V1')) {
    renderRegistro();
    return;
  }''', 1)

p.write_text(html, encoding="utf-8")

# ── Server / API payload ─────────────────────────────────────────────────────
p = Path("servidor_local.py")
server = p.read_text(encoding="utf-8")
server = replace_once(server,
'''    stop_loss = round(price * 0.95, 4) if price is not None else None
    tp1 = round(price + atr, 4) if price is not None and atr is not None else None
    tp2 = round(price + 1.5 * atr, 4) if price is not None and atr is not None else None''',
'''    stop_loss = round(price * 0.95, 4) if price is not None else None
    tp_v1 = round(price + 0.75 * atr, 4) if price is not None and atr is not None else None''', "server risk calculations")

server = replace_once(server,
'''            "full_setup": _bool(row.get("full_setup")),
            "combined_score": _float(row.get("combined_score")),''',
'''            "full_setup": _bool(row.get("full_setup")),
            "strategy_version": row.get("sidi_strategy_version") or "SIDI_SHADOW_V1",
            "gate_failures": row.get("sidi_gate_failures") or None,
            "combined_score": _float(row.get("combined_score")),''', "server selection metadata")

server = replace_once(server,
'''            "vix": _float(row.get("vix")),
        },''',
'''            "vix": _float(row.get("vix")),
            "spy_return_20d_pct": _float(row.get("spy_return_20d")),
            "abnormal_return_20d_pct": _float(row.get("abnormal_return_20d")),
            "sidi_context_ready": _bool(row.get("sidi_context_ready")),
        },''', "server market context")

server = replace_once(server,
'''        "risk_plan": {
            "stop_loss_pct": -5.0,
            "stop_loss": stop_loss,
            "target_tp1_1x_atr": tp1,
            "target_tp2_1_5x_atr": tp2,
        },''',
'''        "risk_plan": {
            "strategy_version": row.get("sidi_strategy_version") or "SIDI_SHADOW_V1",
            "entry_rule": row.get("sidi_entry_rule") or "NEXT_SESSION_OPEN",
            "entry_status": row.get("sidi_entry_status") or "PENDING_NEXT_OPEN",
            "atr14_signal": _float(row.get("sidi_atr14_signal") or row.get("atr_14")),
            "target_atr_multiple": _float(row.get("sidi_tp_atr_mult"), 0.75),
            "stop_loss_pct": _float(row.get("sidi_sl_pct"), -5.0),
            "time_stop_sessions": _int(row.get("sidi_time_stop_sessions"), 7),
            "risk_pct": _float(row.get("sidi_risk_pct"), 1.5),
            "max_positions": _int(row.get("sidi_max_positions"), 5),
            "gap_stop_rule": row.get("sidi_gap_stop_rule") or "EXIT_AT_OPEN_IF_OPEN_BELOW_SL",
            "intraday_conflict_rule": row.get("sidi_intraday_conflict_rule") or "SL_FIRST",
            "stop_loss_indicative_from_signal_close": stop_loss,
            "target_indicative_from_signal_close": tp_v1,
            "note": "Exact TP/SL are fixed from actual Open T+1",
        },''', "server risk plan")

p.write_text(server, encoding="utf-8")
print("SIDI_SHADOW_V1 UI/API migration applied successfully")
