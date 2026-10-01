from pathlib import Path


HTML = Path(__file__).with_name("stock-radar-v3.html")


def _html():
    return HTML.read_text(encoding="utf-8")


def test_v6_navigation_has_registry_and_no_candidates():
    html = _html()
    assert "const CC_SCHEMA_VERSION=6" in html
    assert 'data-ccview="registry"' in html
    assert 'id="view-registry"' in html
    assert 'data-ccview="candidates"' not in html
    assert 'id="view-candidates"' not in html
    assert 'data-section="registro"' not in html


def test_work_prompt_preserves_signal_date_and_uses_intraday_v2():
    html = _html()
    assert '"signal_date": "YYYY-MM-DD (copiar sin cambios del JSON de entrada)"' in html
    assert "forma parte de la clave única del setup" in html
    assert '"strategy_version": "SIDI_INTRADAY_V2"' in html
    assert '"entry_rule": "POST_ANALYSIS_ACTUAL_FILL"' in html
    assert "actual_fill_eur" in html


def test_operations_reference_setup_instead_of_copying_analysis():
    html = _html()
    assert "state.trades.push({id:uid('O'),setupId:a.id,status:'PLANNED'" in html
    assert "...operationViews().map" in html
    assert "state.analyses.forEach(a=>{let s=a.shadow||{},op=liveForSetup(a)" in html


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
    assert "label:'Entrada'" in html
    assert "label:'TP'" in html
    assert "label:'SL'" in html
    assert "color:'blue'" in html
    assert "color:'green'" in html
    assert "color:'red'" in html
