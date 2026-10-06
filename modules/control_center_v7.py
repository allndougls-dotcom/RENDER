"""Modelo normalizado del Control Center V7.

La tabla JSON histórica se mantiene como capa de compatibilidad de la UI, pero
Turso conserva aquí la fuente auditable: un episodio por oportunidad, sus
revisiones diarias, noticias y una única operación Shadow independiente de LIVE.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


SCHEMA_VERSION = 7
ACTIVE_EPISODE_STATUSES = {"PENDING_ENTRY", "OPEN", "TP1"}


def _iso(value=None):
    value = value or datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _hash(*parts, size=20):
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:size]


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sidi_setup_episodes (
            episode_id TEXT PRIMARY KEY,
            strategy_version TEXT NOT NULL,
            ticker TEXT NOT NULL,
            company TEXT,
            sector TEXT,
            subsector TEXT,
            first_signal_date TEXT NOT NULL,
            latest_signal_date TEXT NOT NULL,
            status TEXT NOT NULL,
            current_verdict TEXT,
            current_news_score REAL,
            current_analysis_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            closed_at TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sidi_episode_lookup
        ON sidi_setup_episodes(strategy_version, ticker, status, created_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sidi_analysis_revisions (
            revision_id TEXT PRIMARY KEY,
            episode_id TEXT NOT NULL,
            analysis_key TEXT NOT NULL UNIQUE,
            signal_date TEXT NOT NULL,
            analysis_date TEXT NOT NULL,
            news_score REAL,
            verdict TEXT,
            confidence TEXT,
            data_quality TEXT,
            payload_json TEXT NOT NULL,
            sources_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sidi_revisions_episode
        ON sidi_analysis_revisions(episode_id, signal_date)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sidi_news_items (
            news_id TEXT PRIMARY KEY,
            revision_id TEXT NOT NULL,
            episode_id TEXT NOT NULL,
            item_key TEXT NOT NULL,
            published_at TEXT,
            source TEXT,
            title TEXT,
            url TEXT,
            impact TEXT,
            summary TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(revision_id, item_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sidi_shadow_trades_v2 (
            episode_id TEXT PRIMARY KEY,
            strategy_version TEXT NOT NULL,
            ticker TEXT NOT NULL,
            status TEXT NOT NULL,
            original_verdict TEXT,
            latest_verdict TEXT,
            analysis_completed_at TEXT NOT NULL,
            entry_rule TEXT NOT NULL,
            entry_date TEXT,
            entry_ts TEXT,
            entry_price_usd REAL,
            entry_price_eur REAL,
            fx_usd_per_eur REAL,
            atr14_usd REAL,
            atr14_eur REAL,
            stop_price_usd REAL,
            tp1_price_usd REAL,
            tp2_price_usd REAL,
            remaining_stop_usd REAL,
            shares INTEGER,
            risk_eur REAL,
            sessions_held INTEGER DEFAULT 0,
            tp1_hit INTEGER DEFAULT 0,
            tp1_date TEXT,
            exit_date TEXT,
            exit_ts TEXT,
            exit_price_usd REAL,
            exit_reason TEXT,
            pnl_pct_net REAL,
            pnl_eur_net REAL,
            r_multiple_net REAL,
            mfe_pct REAL,
            mae_pct REAL,
            last_mark_date TEXT,
            last_processed_ts TEXT,
            invalid_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sidi_shadow_marks_v2 (
            episode_id TEXT NOT NULL,
            session_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            mfe_pct REAL,
            mae_pct REAL,
            touched_sl INTEGER DEFAULT 0,
            touched_tp1 INTEGER DEFAULT 0,
            touched_tp2 INTEGER DEFAULT 0,
            source TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(episode_id, session_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sidi_control_center_backups (
            backup_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()


def backup_state(conn, state, schema_version, reason="V7_MIGRATION"):
    ensure_schema(conn)
    now = _iso()
    backup_id = "B-" + _hash(now, reason)
    conn.execute(
        "INSERT OR IGNORE INTO sidi_control_center_backups "
        "(backup_id, schema_version, state_json, reason, created_at) VALUES (?,?,?,?,?)",
        (backup_id, int(schema_version or 0), _json(state), reason, now),
    )
    conn.commit()
    return backup_id


def _active_episode(conn, strategy_version, ticker):
    placeholders = ",".join("?" for _ in ACTIVE_EPISODE_STATUSES)
    row = conn.execute(
        "SELECT episode_id FROM sidi_setup_episodes "
        f"WHERE strategy_version=? AND ticker=? AND status IN ({placeholders}) "
        "ORDER BY created_at DESC LIMIT 1",
        (strategy_version, ticker, *sorted(ACTIVE_EPISODE_STATUSES)),
    ).fetchone()
    return row[0] if row else None


def _news_items(position):
    items = position.get("news_items") or position.get("news") or []
    return items if isinstance(items, list) else []


def upsert_work_position(
    conn,
    normalized,
    raw_position,
    analysis_date,
    sources=None,
    completed_at=None,
    actionable=True,
    timing_reason="ACTIONABLE_INTRADAY",
):
    """Crea/revisa un episodio y garantiza una Shadow para toda FULL V2."""
    ensure_schema(conn)
    strategy = str(normalized.get("strategyVersion") or "").upper()
    ticker = str(normalized.get("ticker") or "").upper()
    signal_date = str(normalized.get("signalDate") or "")[:10]
    completed = _iso(completed_at)
    analysis_key = f"{strategy}|{ticker}|{signal_date}"
    episode_id = _active_episode(conn, strategy, ticker)
    created = False
    if not episode_id:
        episode_id = "E-" + _hash(strategy, ticker, signal_date)
        created = True

    payload_json = _json(raw_position)
    if created:
        conn.execute("""
            INSERT OR IGNORE INTO sidi_setup_episodes
            (episode_id, strategy_version, ticker, company, sector, subsector,
             first_signal_date, latest_signal_date, status, current_verdict,
             current_news_score, current_analysis_json, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            episode_id, strategy, ticker, normalized.get("company"),
            normalized.get("sector"), normalized.get("subsector"), signal_date,
            signal_date, "PENDING_ENTRY", normalized.get("verdict"),
            normalized.get("newsScore"), payload_json, completed, completed,
        ))
    else:
        conn.execute("""
            UPDATE sidi_setup_episodes SET latest_signal_date=?, company=?, sector=?,
            subsector=?, current_verdict=?, current_news_score=?,
            current_analysis_json=?, updated_at=? WHERE episode_id=?
        """, (
            signal_date, normalized.get("company"), normalized.get("sector"),
            normalized.get("subsector"), normalized.get("verdict"),
            normalized.get("newsScore"), payload_json, completed, episode_id,
        ))

    revision_id = "R-" + _hash(analysis_key)
    conn.execute("""
        INSERT INTO sidi_analysis_revisions
        (revision_id, episode_id, analysis_key, signal_date, analysis_date,
         news_score, verdict, confidence, data_quality, payload_json,
         sources_json, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(analysis_key) DO UPDATE SET
          episode_id=excluded.episode_id, analysis_date=excluded.analysis_date,
          news_score=excluded.news_score, verdict=excluded.verdict,
          confidence=excluded.confidence, data_quality=excluded.data_quality,
          payload_json=excluded.payload_json, sources_json=excluded.sources_json,
          updated_at=excluded.updated_at
    """, (
        revision_id, episode_id, analysis_key, signal_date, analysis_date,
        normalized.get("newsScore"), normalized.get("verdict"),
        normalized.get("confidence"), normalized.get("dataQuality"), payload_json,
        _json(sources or []), completed, completed,
    ))

    for index, item in enumerate(_news_items(raw_position)):
        if not isinstance(item, dict):
            continue
        item_key = item.get("url") or item.get("title") or f"item-{index}"
        news_id = "N-" + _hash(revision_id, item_key)
        conn.execute("""
            INSERT INTO sidi_news_items
            (news_id, revision_id, episode_id, item_key, published_at, source,
             title, url, impact, summary, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(revision_id, item_key) DO UPDATE SET
              published_at=excluded.published_at, source=excluded.source,
              title=excluded.title, url=excluded.url, impact=excluded.impact,
              summary=excluded.summary
        """, (
            news_id, revision_id, episode_id, str(item_key),
            item.get("published_at") or item.get("date"), item.get("source"),
            item.get("title"), item.get("url"), item.get("impact"),
            item.get("summary"), completed,
        ))

    shadow = conn.execute(
        "SELECT status FROM sidi_shadow_trades_v2 WHERE episode_id=?", (episode_id,)
    ).fetchone()
    if not shadow:
        atr_usd = normalized.get("atr14Usd") or 0.0
        atr_eur = normalized.get("atr14Eur") or 0.0
        price_usd = normalized.get("entryReferenceUsd") or normalized.get("priceUsd") or 0.0
        price_eur = normalized.get("entryReferenceEur") or normalized.get("priceEur") or 0.0
        fx = price_usd / price_eur if price_usd and price_eur else 0.0
        if strategy != "SIDI_INTRADAY_V2":
            status, invalid = "INVALID_LEGACY", "LEGACY_VERSION_ARCHIVED"
        elif not actionable:
            status, invalid = "INVALID_LATE", timing_reason
        elif not atr_usd:
            status, invalid = "INVALID_NO_ATR", "MISSING_ATR_USD"
        elif not fx:
            status, invalid = "INVALID_NO_FX", "MISSING_USD_EUR_REFERENCE"
        else:
            status, invalid = "PENDING_ENTRY", None
        conn.execute("""
            INSERT INTO sidi_shadow_trades_v2
            (episode_id, strategy_version, ticker, status, original_verdict,
             latest_verdict, analysis_completed_at, entry_rule,
             fx_usd_per_eur, atr14_usd, atr14_eur, sessions_held,
             invalid_reason, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            episode_id, strategy, ticker, status, normalized.get("verdict"),
            normalized.get("verdict"), completed, "FIRST_5M_BAR_AFTER_ANALYSIS",
            fx, atr_usd, atr_eur, 0, invalid, completed, completed,
        ))
        if status.startswith("INVALID"):
            conn.execute(
                "UPDATE sidi_setup_episodes SET status=?, closed_at=?, updated_at=? "
                "WHERE episode_id=?",
                (status, completed, completed, episode_id),
            )
    else:
        conn.execute(
            "UPDATE sidi_shadow_trades_v2 SET latest_verdict=?, updated_at=? "
            "WHERE episode_id=?",
            (normalized.get("verdict"), completed, episode_id),
        )
    conn.commit()
    return {
        "episode_id": episode_id,
        "analysis_key": analysis_key,
        "created": created,
        "revision_id": revision_id,
    }


def hydrate_analysis(conn, analysis):
    episode_id = analysis.get("episodeId")
    if not episode_id:
        return analysis
    ensure_schema(conn)
    columns = [
        "status", "original_verdict", "latest_verdict",
        "analysis_completed_at", "entry_date", "entry_ts",
        "entry_price_usd", "entry_price_eur", "stop_price_usd",
        "tp1_price_usd", "tp2_price_usd", "shares", "risk_eur",
        "sessions_held", "tp1_hit", "exit_date", "exit_price_usd",
        "exit_reason", "pnl_pct_net", "pnl_eur_net", "r_multiple_net",
        "mfe_pct", "mae_pct", "last_mark_date", "invalid_reason",
    ]
    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM sidi_shadow_trades_v2 WHERE episode_id=?",
        (episode_id,),
    ).fetchone()
    analysis["shadow"] = dict(zip(columns, row)) if row else None
    revisions = conn.execute("""
        SELECT revision_id, signal_date, analysis_date, news_score, verdict,
               confidence, data_quality, payload_json, created_at
        FROM sidi_analysis_revisions WHERE episode_id=?
        ORDER BY signal_date, created_at
    """, (episode_id,)).fetchall()
    analysis["revisions"] = []
    latest_revision_id = None
    for revision in revisions:
        payload = json.loads(revision[7]) if revision[7] else {}
        analysis["revisions"].append({
            "revisionId": revision[0], "signalDate": revision[1],
            "analysisDate": revision[2], "newsScore": revision[3],
            "verdict": revision[4], "confidence": revision[5],
            "dataQuality": revision[6], "payload": payload,
            "createdAt": revision[8],
        })
        latest_revision_id = revision[0]
    analysis["revisionCount"] = len(analysis["revisions"])
    news_rows = conn.execute("""
        SELECT published_at, source, title, url, impact, summary
        FROM sidi_news_items WHERE episode_id=?
        AND (? IS NULL OR revision_id=?) ORDER BY published_at DESC, title
    """, (episode_id, latest_revision_id, latest_revision_id)).fetchall()
    analysis["newsItems"] = [dict(zip(
        ["publishedAt", "source", "title", "url", "impact", "summary"], row
    )) for row in news_rows]
    return analysis


def episode_status_counts(conn):
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM sidi_setup_episodes GROUP BY status ORDER BY status"
    ).fetchall()
    return {row[0]: row[1] for row in rows}
