import sqlite3
from datetime import datetime, timedelta, timezone

from modules.control_center_v7 import ensure_schema, hydrate_analysis, upsert_work_position
from modules.shadow_intraday_v2 import replay_shadow


def _conn():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    return conn


def _normalized(verdict="WATCHLIST", signal_date="2026-10-01"):
    return {
        "strategyVersion": "SIDI_INTRADAY_V2",
        "ticker": "AAA",
        "signalDate": signal_date,
        "company": "Alpha",
        "sector": "Industrials",
        "subsector": "Machinery",
        "verdict": verdict,
        "newsScore": 6.2,
        "confidence": "alta",
        "dataQuality": "alta",
        "priceUsd": 100.0,
        "priceEur": 90.0,
        "entryReferenceUsd": 100.0,
        "entryReferenceEur": 90.0,
        "atr14Usd": 4.0,
        "atr14Eur": 3.6,
    }


def _raw(signal_date="2026-10-01"):
    return {
        "ticker": "AAA",
        "signal_date": signal_date,
        "news_items": [{
            "published_at": "2026-10-01", "source": "Primary",
            "title": "Results", "url": "https://example.com/results",
            "impact": "POSITIVO", "summary": "Guidance maintained.",
        }],
        "strengths": ["Cash flow"],
        "weaknesses": ["Leverage"],
    }


def test_daily_revisions_share_one_active_episode_and_one_shadow():
    conn = _conn()
    completed = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
    first = upsert_work_position(
        conn, _normalized(), _raw(), "2026-10-01", completed_at=completed,
    )
    second = upsert_work_position(
        conn, _normalized("VALIDADA", "2026-10-02"), _raw("2026-10-02"),
        "2026-10-02", completed_at=completed + timedelta(days=1),
    )
    assert first["episode_id"] == second["episode_id"]
    assert conn.execute("SELECT COUNT(*) FROM sidi_setup_episodes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sidi_analysis_revisions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM sidi_shadow_trades_v2").fetchone()[0] == 1
    shadow = conn.execute(
        "SELECT original_verdict, latest_verdict FROM sidi_shadow_trades_v2"
    ).fetchone()
    assert shadow == ("WATCHLIST", "VALIDADA")


def test_full_watchlist_also_receives_shadow_and_structured_news():
    conn = _conn()
    meta = upsert_work_position(
        conn, _normalized("WATCHLIST"), _raw(), "2026-10-01",
        completed_at=datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc),
    )
    shadow = conn.execute(
        "SELECT status FROM sidi_shadow_trades_v2 WHERE episode_id=?",
        (meta["episode_id"],),
    ).fetchone()
    assert shadow[0] == "PENDING_ENTRY"
    assert conn.execute("SELECT COUNT(*) FROM sidi_news_items").fetchone()[0] == 1
    analysis = hydrate_analysis(conn, {"episodeId": meta["episode_id"]})
    assert analysis["revisionCount"] == 1
    assert analysis["newsItems"][0]["title"] == "Results"


def _shadow():
    return {
        "episode_id": "E-1", "ticker": "AAA", "status": "PENDING_ENTRY",
        "analysis_completed_at": "2026-10-01T14:01:00+00:00",
        "entry_price_usd": None, "entry_ts": None, "fx_usd_per_eur": 1.1,
        "atr14_usd": 4.0, "sessions_held": 0, "tp1_hit": 0,
        "created_at": "2026-10-01T14:01:00+00:00",
    }


def _bar(minutes, open_, high, low, close, day=1):
    return {
        "ts": datetime(2026, 10, day, 14, 5, tzinfo=timezone.utc) + timedelta(minutes=minutes),
        "open": open_, "high": high, "low": low, "close": close,
    }


def test_intraday_shadow_uses_first_bar_after_analysis_and_sl_first():
    shadow, marks = replay_shadow(_shadow(), [
        _bar(-5, 99, 101, 98, 100),
        _bar(0, 100, 105, 94, 101),
    ])
    assert shadow["entry_price_usd"] == 100
    assert shadow["exit_reason"] == "SL"
    assert shadow["exit_price_usd"] == 95
    assert marks[0]["touched_sl"] == 1


def test_intraday_shadow_tp1_moves_remaining_stop_to_break_even():
    shadow, _ = replay_shadow(_shadow(), [
        _bar(0, 100, 104.1, 99.5, 103),
        _bar(5, 103, 103.5, 99.0, 100),
    ])
    assert shadow["tp1_hit"] == 1
    assert shadow["exit_reason"] == "TP1_BE"
    assert shadow["exit_price_usd"] == 100
    assert shadow["pnl_eur_net"] > 0


def test_intraday_shadow_does_not_reprocess_a_saved_bar():
    shadow, _ = replay_shadow(_shadow(), [
        _bar(0, 100, 102, 99, 101),
    ])
    sessions = shadow["sessions_held"]
    saved_ts = shadow["last_processed_ts"]
    shadow, marks = replay_shadow(shadow, [
        _bar(0, 100, 102, 99, 101),
    ])
    assert marks == []
    assert shadow["sessions_held"] == sessions
    assert shadow["last_processed_ts"] == saved_ts


def test_pending_shadow_expires_if_same_session_has_no_entry_bar():
    shadow, marks = replay_shadow(
        _shadow(), [], as_of=datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)
    )
    assert marks == []
    assert shadow["status"] == "INVALID_NO_ENTRY"
    assert shadow["invalid_reason"] == "NO_5M_BAR_AFTER_ANALYSIS_SAME_SESSION"


def test_mfe_stops_at_exit_bar_instead_of_using_later_prices():
    shadow, _ = replay_shadow(_shadow(), [
        _bar(0, 100, 101, 94, 95),
        _bar(5, 95, 120, 94, 119),
    ])
    assert shadow["exit_reason"] == "SL"
    assert shadow["mfe_pct"] == 1.0
