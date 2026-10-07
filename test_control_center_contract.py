from pathlib import Path


HTML = Path(__file__).with_name("stock-radar-v3.html")


def _html():
    return HTML.read_text(encoding="utf-8")


def test_v7_navigation_has_registry_and_no_candidates():
    html = _html()
    assert "const CC_SCHEMA_VERSION=7" in html
    assert 'data-ccview="registry"' in html
    assert 'id="view-registry"' in html
    assert 'data-ccview="candidates"' not in html
    assert 'id="view-candidates"' not in html
    assert 'data-section="registro"' not in html


def test_work_prompt_preserves_signal_date_and_uses_intraday_v2():
    html = _html()
    assert '"signal_date": "YYYY-MM-DD (copiar sin cambios del JSON de entrada)"' in html
    assert "identifica la revisión diaria" in html
    assert "episodio mientras la oportunidad siga activa" in html
    assert '"strategy_version": "SIDI_INTRADAY_V2"' in html
    assert '"entry_rule": "POST_ANALYSIS_ACTUAL_FILL"' in html
    assert "actual_fill_eur" in html
    assert '"atr14_signal_usd": 0.0' in html
    assert '"news_items": [' in html


def test_operations_reference_setup_instead_of_copying_analysis():
    html = _html()
    assert "state.trades.push({id:uid('O'),setupId:a.id,status:'PLANNED'" in html
    assert "...operationViews().map" in html
    assert "state.analyses.forEach(a=>{let s=a.shadow||{},op=liveForSetup(a)" in html


def test_manual_historical_trade_form_updates_live_history():
    html = _html()
    assert 'onclick="openHistoricalTrade()"' in html
    assert 'id="tradeHistoryBody"' in html
    assert "source:'HISTORICAL_MANUAL'" in html
    assert "status:'CLOSED'" in html
    assert "exitDate<entryDate" in html
    assert "exitDate>today()" in html
    assert "tp1Shares>=shares" in html
    assert "window.saveHistoricalTrade = saveHistoricalTrade" in html


def test_registry_bindings_replace_candidate_and_history_bindings():
    html = _html()
    assert "$('registrySearch').oninput=renderRegistry" in html
    assert "$('registryVerdict').onchange=renderRegistry" in html
    assert "$('registryResult').onchange=renderRegistry" in html
    assert "$('candidateSearch').oninput" not in html
    assert "$('historySearch').oninput" not in html


def test_chart_contract_contains_entry_tp_and_sl():
    html = _html()
    assert "a.shadow.entry_open" in html
    assert "a.shadow.target_price" in html
    assert "a.shadow.stop_price" in html
    assert "a.shadow.entry_price_usd" in html
    assert "a.shadow.tp1_price_usd" in html
    assert "a.shadow.stop_price_usd" in html
    assert "label:'Entrada'" in html
    assert "'TP1':'TP'" in html
    assert "label:'TP2'" in html
    assert "label:'SL'" in html
    assert "color:'blue'" in html
    assert "color:'green'" in html
    assert "color:'red'" in html


def test_v7_prompt_contains_episode_shadow_and_source_contract():
    html = _html()
    assert '"actual_fill_usd": null' in html
    assert '"research_sources": {' in html
    assert "Control Center V7" in html
    assert "primera vela de 5 minutos posterior al análisis" in html


def test_update_button_dispatches_github_without_exposing_a_secret():
    html = _html()
    assert "fetch('/trigger'" in html
    assert "fetch('/api/ingesta/status'" in html
    assert "INGESTA_TOKEN_KEY = 'sidiUpdateToken'" in html
    assert "const INGESTA_TOKEN = 'stock-radar-2026'" not in html
    assert "Lanza el workflow de ingesta en GitHub Actions" in html


def test_control_center_waits_for_render_and_keeps_turso_authoritative():
    html = _html()
    assert "CC_READ_TIMEOUT_MS=30000" in html
    assert "CC_WRITE_TIMEOUT_MS=30000" in html
    assert "async function ccFetch(" in html
    assert "Render está despertando · reintentando conexión con Turso" in html
    assert "state=migrateState(remote.state)" in html
    assert "localTs=state.updatedAt" not in html
    assert "Turso tarda en responder · la base maestra no se ha sustituido" in html
