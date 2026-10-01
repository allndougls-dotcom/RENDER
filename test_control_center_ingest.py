import copy
import sqlite3
from datetime import datetime, timezone

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
    conn.execute("""
        CREATE TABLE sidi_shadow_signals (
            ticker TEXT, signal_date TEXT, strategy_version TEXT, status TEXT,
            entry_date TEXT, entry_open REAL, stop_price REAL, target_price REAL,
            shares INTEGER, risk_eur REAL, sessions_held INTEGER, exit_date TEXT,
            exit_price REAL, exit_reason TEXT, pnl_pct_net REAL, pnl_eur_net REAL,
            r_multiple_net REAL, capital_after REAL
        )
    """)
    return conn


def _payload(verdict="VALIDADA"):
    return {
        "sidi_excel_payload": {
            "analysis_date": "2026-09-28",
            "positions": [{
                "ticker": "AAA",
                "signal_date": "2026-09-28",
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
        assert len(state["trades"]) == 2
        assert any(item["id"] == "T-1" for item in state["trades"])
        linked = next(item for item in state["trades"] if item.get("setupId"))
        assert linked["status"] == "PLANNED"
        assert "ticker" not in linked
        assert "analysisVerdict" not in linked
        assert state["analyses"][0]["analysisSource"] == "CHATGPT_WORK"
        assert state["analyses"][0]["route"] == "OPERATIONS"
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


def test_t1_analysis_is_on_time_before_new_york_open():
    before_open = datetime(2026, 9, 29, 13, 29, tzinfo=timezone.utc)
    at_open = datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc)
    assert server._analysis_is_on_time("2026-09-28", "2026-09-29", before_open)
    assert not server._analysis_is_on_time("2026-09-28", "2026-09-29", at_open)


def test_t1_skips_weekends_and_nyse_good_friday():
    before_open = datetime(2026, 4, 6, 13, 0, tzinfo=timezone.utc)
    assert server._next_nyse_session(datetime(2026, 4, 2).date()).isoformat() == "2026-04-06"
    assert server._analysis_is_on_time("2026-04-02", "2026-04-06", before_open)


def test_v4_migration_links_operation_and_removes_copied_analysis_fields():
    state = server._migrate_control_center_state({
        "schemaVersion": 4,
        "settings": {},
        "analyses": [{
            "id": "A-1", "date": "2026-09-28", "ticker": "aaa",
            "strategyVersion": "SIDI_SHADOW_V1", "verdict": "VALIDADA",
        }],
        "trades": [{
            "id": "T-1", "analysisId": "A-1", "status": "OPEN",
            "ticker": "AAA", "analysisVerdict": "VALIDADA",
            "analysisNewsScore": 7.5, "strategyVersion": "SIDI_SHADOW_V1",
        }],
    })
    analysis, operation = state["analyses"][0], state["trades"][0]
    assert state["schemaVersion"] == 6
    assert analysis["setupKey"] == "SIDI_SHADOW_V1|AAA|2026-09-28"
    assert analysis["route"] == "OPERATIONS"
    assert operation["setupId"] == "A-1"
    assert "analysisId" not in operation
    assert "ticker" not in operation
    assert "analysisVerdict" not in operation
    assert "analysisNewsScore" not in operation


def test_same_ticker_with_different_signal_dates_creates_two_setups():
    conn = _memory_connection()
    original = server.get_turso_conn
    server.get_turso_conn = lambda: conn
    try:
        first = _payload("WATCHLIST")
        second = copy.deepcopy(first)
        second["sidi_excel_payload"]["positions"][0]["signal_date"] = "2026-09-27"
        server.control_center_upsert_analysis_payload(first)
        server.control_center_upsert_analysis_payload(second)
        state = server.control_center_get()["state"]
        assert len(state["analyses"]) == 2
        assert len({item["setupKey"] for item in state["analyses"]}) == 2
    finally:
        server.get_turso_conn = original


def test_non_full_v1_is_not_registered():
    conn = _memory_connection()
    original = server.get_turso_conn
    server.get_turso_conn = lambda: conn
    try:
        payload = _payload()
        payload["sidi_excel_payload"]["positions"][0]["full_setup"] = False
        result = server.control_center_upsert_analysis_payload(payload)
        state = server.control_center_get()["state"]
        assert result["ignored_non_full"] == ["AAA"]
        assert state["analyses"] == []
        assert state["trades"] == []
    finally:
        server.get_turso_conn = original


def test_v1_without_signal_date_is_rejected_instead_of_inventing_a_key():
    conn = _memory_connection()
    original = server.get_turso_conn
    server.get_turso_conn = lambda: conn
    try:
        payload = _payload()
        del payload["sidi_excel_payload"]["positions"][0]["signal_date"]
        try:
            server.control_center_upsert_analysis_payload(payload)
        except ValueError as error:
            assert "signal_date obligatorio" in str(error)
        else:
            raise AssertionError("El JSON V1 sin signal_date debía rechazarse")
        assert server.control_center_get() is None
    finally:
        server.get_turso_conn = original


def test_late_analysis_is_archived_and_not_made_operable():
    conn = _memory_connection()
    original = server.get_turso_conn
    server.get_turso_conn = lambda: conn
    try:
        payload = _payload()
        payload["sidi_excel_payload"]["positions"][0]["signal_date"] = "2026-09-27"
        server.control_center_upsert_analysis_payload(payload)
        state = server.control_center_get()["state"]
        analysis = state["analyses"][0]
        assert analysis["route"] == "REGISTRY"
        assert analysis["routingReason"] == "ANALYSIS_LATE"
        assert analysis["timingStatus"] == "LATE_ANALYSIS"
        assert state["trades"] == []
    finally:
        server.get_turso_conn = original


def test_shadow_is_hydrated_only_for_analyzed_setup():
    conn = _memory_connection()
    original = server.get_turso_conn
    server.get_turso_conn = lambda: conn
    try:
        server.control_center_upsert_analysis_payload(_payload("BLOQUEADA"))
        rows = [
            ("AAA", "2026-09-28", "SIDI_SHADOW_V1", "CLOSED", "2026-09-29", 100, 95, 103, 10, 50, 2, "2026-09-30", 103, "TP", 2.8, 28, .56, 10028),
            ("RAW", "2026-09-28", "SIDI_SHADOW_V1", "CLOSED", "2026-09-29", 50, 47.5, 52, 10, 25, 2, "2026-09-30", 47.5, "SL", -5.2, -26, -1.04, 10002),
        ]
        conn.executemany("INSERT INTO sidi_shadow_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        state = server.control_center_get()["state"]
        assert len(state["analyses"]) == 1
        assert state["analyses"][0]["shadow"]["exit_reason"] == "TP"
        assert all(item["ticker"] != "RAW" for item in state["analyses"])
    finally:
        server.get_turso_conn = original


def test_verdict_revision_reuses_cancelled_plan_without_duplicates():
    conn = _memory_connection()
    original = server.get_turso_conn
    server.get_turso_conn = lambda: conn
    try:
        server.control_center_upsert_analysis_payload(_payload("VALIDADA"))
        server.control_center_upsert_analysis_payload(_payload("WATCHLIST"))
        server.control_center_upsert_analysis_payload(_payload("VALIDADA"))
        state = server.control_center_get()["state"]
        linked = [item for item in state["trades"] if item.get("setupId")]
        assert len(linked) == 1
        assert linked[0]["status"] == "PLANNED"
        assert state["analyses"][0]["route"] == "OPERATIONS"
    finally:
        server.get_turso_conn = original


def test_closed_operation_stays_in_registry_after_analysis_revision():
    conn = _memory_connection()
    original = server.get_turso_conn
    server.get_turso_conn = lambda: conn
    try:
        server.control_center_upsert_analysis_payload(_payload("VALIDADA"))
        state = server.control_center_get()["state"]
        state["trades"][0]["status"] = "CLOSED"
        state["trades"][0]["exitDate"] = "2026-10-02"
        server.control_center_save(state)
        revised = _payload("VALIDADA CON CONDICIONES")
        server.control_center_upsert_analysis_payload(revised)
        state = server.control_center_get()["state"]
        assert len(state["trades"]) == 1
        assert state["trades"][0]["status"] == "CLOSED"
        assert state["analyses"][0]["route"] == "REGISTRY"
        assert state["analyses"][0]["routingReason"] == "LIVE_CLOSED"
    finally:
        server.get_turso_conn = original


def test_intraday_v2_is_actionable_after_ingestion_and_before_entry_cutoff():
    during_market = datetime(2026, 9, 29, 15, 30, tzinfo=timezone.utc)
    actionable, status, reason = server._analysis_route_timing(
        "SIDI_INTRADAY_V2", "2026-09-28", "2026-09-29", during_market
    )
    assert actionable is True
    assert status == "ACTIONABLE_INTRADAY"
    assert reason == "AUTO_VERDICT_OPERABLE_INTRADAY"


def test_intraday_v2_never_creates_retroactive_entry_after_cutoff():
    after_window = datetime(2026, 9, 29, 19, 45, tzinfo=timezone.utc)
    actionable, status, reason = server._analysis_route_timing(
        "SIDI_INTRADAY_V2", "2026-09-28", "2026-09-29", after_window
    )
    assert actionable is False
    assert status == "MARKET_CLOSED"
    assert reason == "ANALYSIS_AFTER_ENTRY_WINDOW"


def test_v6_migration_applies_three_position_limit():
    state = server._migrate_control_center_state({
        "schemaVersion": 5,
        "settings": {"maxPositions": 5, "portfolioRiskPct": 7.5},
        "analyses": [],
        "trades": [],
    })
    assert state["schemaVersion"] == 6
    assert state["settings"]["maxPositions"] == 3
    assert state["settings"]["portfolioRiskPct"] == 4.5
