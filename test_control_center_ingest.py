import sqlite3

import servidor_local as server


def _memory_connection():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE sidi_control_center_state (
            id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    return conn


def _payload(verdict="VALIDADA"):
    return {
        "sidi_excel_payload": {
            "analysis_date": "2026-09-28",
            "positions": [{
                "ticker": "AAA",
                "company": "Alpha",
                "sector": "Industrials",
                "price_current_eur": 90,
                "price_current_usd": 100,
                "atr14_signal": 4,
                "news_score": 7.5,
                "verdict": verdict,
                "strategy_version": "SIDI_SHADOW_V1",
                "full_setup": True,
                "decision": "Open T+1",
                "trade_plan": {
                    "entry_rule": "NEXT_SESSION_OPEN",
                    "max_risk_eur": 150,
                },
            }],
        }
    }


def test_work_payload_is_idempotent_and_preserves_live_trades():
    conn = _memory_connection()
    original = server.get_turso_conn
    server.get_turso_conn = lambda: conn
    try:
        server.control_center_save({
            "settings": dict(server.CONTROL_CENTER_DEFAULT_SETTINGS),
            "analyses": [],
            "trades": [{"id": "T-1", "ticker": "ZZZ", "status": "OPEN"}],
        })
        first = server.control_center_upsert_analysis_payload(_payload())
        second = server.control_center_upsert_analysis_payload(_payload())
        state = server.control_center_get()["state"]
        assert first["inserted"] == ["AAA"]
        assert second["unchanged"] == ["AAA"]
        assert len(state["analyses"]) == 1
        assert state["trades"] == [{"id": "T-1", "ticker": "ZZZ", "status": "OPEN"}]
        assert state["analyses"][0]["analysisSource"] == "CHATGPT_WORK"
    finally:
        server.get_turso_conn = original


def test_work_revision_updates_same_ticker_and_date():
    conn = _memory_connection()
    original = server.get_turso_conn
    server.get_turso_conn = lambda: conn
    try:
        server.control_center_upsert_analysis_payload(_payload("WATCHLIST"))
        result = server.control_center_upsert_analysis_payload(_payload("BLOQUEADA"))
        analysis = server.control_center_get()["state"]["analyses"][0]
        assert result["updated"] == ["AAA"]
        assert analysis["verdict"] == "BLOQUEADA"
        assert analysis["revision"] == 2
    finally:
        server.get_turso_conn = original


def test_atr_is_converted_to_eur_for_v1_execution():
    analysis = server._control_center_analysis(
        _payload()["sidi_excel_payload"]["positions"][0], "2026-09-28"
    )
    assert analysis["atr14Eur"] == 3.6
    assert analysis["entryRule"] == "NEXT_SESSION_OPEN"
    assert analysis["fullSetup"] is True
