"""Build a frozen validation snapshot for SIDI robustness tests.

The snapshot is deliberately broader than the current frozen strategy so future
one-at-a-time perturbations can be evaluated without re-downloading data:
- technical signals with DD60 >= 10% (same RSI/MACD/volume setup)
- one-sided historical membership correction
- PIT fundamental score + coverage for every technical signal date
- context features needed by the frozen strategy (SPY20 / abnormal20 / etc.)
- OHLCV prices + indicators used by the execution engine

No production rule is changed. The output is a gzip-compressed pickle artifact
plus a human-readable manifest and signal audit CSV.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import pickle
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import backtest as bt
import backtest_experiments as exp
import historical_membership as membership
import calibrate_pit_proxy_ttm as cal_ttm
import pit_proxy_ttm_fast as ttm_fast
import analyze_signal_context_features as feat
import backtest_context_filters as ctxmod
import backtest_context_filters_pit_oos as pit

SNAPSHOT_VERSION = "SIDI_VALIDATION_SNAPSHOT_V1"
SIGNAL_START = "2023-01-01"
END = "2026-09-10"
PRICE_START = "2022-01-01"
BROAD_DD_MIN = 10.0
MIN_COVERAGE = 6
SNAPSHOT_PATH = Path("sidi_validation_snapshot_v1.pkl.gz")
MANIFEST_PATH = Path("sidi_validation_snapshot_v1_manifest.json")
AUDIT_PATH = Path("sidi_validation_snapshot_v1_signals.csv")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main():
    print("=" * 118)
    print(f"BUILD {SNAPSHOT_VERSION}")
    print("=" * 118)
    print("Broad technical universe: DD60>=10%, coverage>=6/8. No fund/context threshold yet.\n")

    _, sectors = feat.load_master()
    tickers = bt.load_tickers()
    current_fund_scores = bt.load_fundamental_scores()
    added = membership.load_date_added()

    prices = bt.download_prices(tickers, start_date=PRICE_START, end_date=END)
    tickers_ok = [t for t in tickers if t in prices and prices[t] is not None and not prices[t].empty]
    prices = {t: prices[t] for t in tickers_ok}
    indicators = bt.build_indicators(prices)
    prices_idx = {t: feat.to_indexed(d) for t, d in prices.items()}
    pmap = pit.price_maps(prices)

    spec = exp.signal_variant(BROAD_DD_MIN, False)
    spec["min_fund_score"] = 0.0
    raw, nraw = bt.build_signal_map(
        indicators, spec, spy_dict={}, date_from=SIGNAL_START, date_to=END,
        fund_scores=current_fund_scores, sector_regime=None,
    )
    tech_map, member_stats = membership.filter_signal_map(raw, added)
    signal_dates = sorted({d for sigs in tech_map.values() for d in sigs})
    print(
        f"Technical raw={nraw}; membership-kept={member_stats['kept']}; "
        f"removed={member_stats['removed_pre_membership']}; signal dates={len(signal_dates)}"
    )

    print("\nBuilding quarterly/TTM PIT fundamentals for broad signal universe...")
    statements, statement_failures = cal_ttm.fetch_full_statements(tickers_ok)
    state_cache = ttm_fast.build_state_cache(statements, SIGNAL_START, END)
    scores, coverage = pit.dynamic_scores(signal_dates, tickers_ok, sectors, state_cache, pmap, added)

    eligible_map = {}
    audit = []
    filter_stats = {"technical": 0, "missing_score": 0, "low_coverage": 0, "kept": 0}
    for ticker, sigs in tech_map.items():
        for d, sig in sigs.items():
            filter_stats["technical"] += 1
            rec = scores.get(d, {}).get(ticker)
            if rec is None:
                filter_stats["missing_score"] += 1
                continue
            if int(rec.get("coverage", 0)) < MIN_COVERAGE:
                filter_stats["low_coverage"] += 1
                continue
            item = dict(sig)
            item["pit_fund_score"] = float(rec["fund_score"])
            item["pit_coverage"] = int(rec["coverage"])
            eligible_map.setdefault(ticker, {})[d] = item
            filter_stats["kept"] += 1

    print("Coverage-only filter:", json.dumps(filter_stats, indent=2))

    feat.DOWNLOAD_START = PRICE_START
    refs = {
        s: feat.download_reference(s)
        for s in sorted(set(feat.SECTOR_ETF.values()) | {"SPY", "^VIX"})
    }
    print("Computing breadth/context once for frozen broad signal universe...")
    market_breadth, sector_breadth = feat.breadth_maps(prices_idx, sectors, added)
    context = ctxmod.build_context(
        eligible_map, prices_idx, sectors, refs, market_breadth, sector_breadth
    )

    for ticker, sigs in eligible_map.items():
        for d, sig in sigs.items():
            c = context.get((ticker, d), {})
            audit.append({
                "ticker": ticker,
                "signal_date": d,
                "dd": sig.get("dd"),
                "rsi": sig.get("rsi"),
                "atr_exact": sig.get("atr_exact"),
                "pit_fund_score": sig.get("pit_fund_score"),
                "pit_coverage": sig.get("pit_coverage"),
                "spy20": c.get("spy20"),
                "abnormal20": c.get("abnormal20"),
                "vix_pct": c.get("vix_pct"),
                "sector_breadth": c.get("sector_breadth"),
                "vol_ratio": c.get("vol_ratio"),
            })

    snapshot = {
        "version": SNAPSHOT_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "signal_start": SIGNAL_START,
        "end": END,
        "price_start": PRICE_START,
        "broad_dd_min": BROAD_DD_MIN,
        "min_coverage": MIN_COVERAGE,
        "prices": prices,
        "indicators": indicators,
        "current_fund_scores": current_fund_scores,
        "signals": eligible_map,
        "context": context,
        "sectors": sectors,
        "date_added": added,
        "coverage": coverage,
        "meta": {
            "input_tickers": len(tickers),
            "price_tickers": len(tickers_ok),
            "technical_raw": nraw,
            "membership": member_stats,
            "coverage_filter": filter_stats,
            "statement_failures": statement_failures,
            "context_records": len(context),
        },
    }

    with gzip.open(SNAPSHOT_PATH, "wb", compresslevel=6) as f:
        pickle.dump(snapshot, f, protocol=pickle.HIGHEST_PROTOCOL)

    audit_df = pd.DataFrame(audit).sort_values(["signal_date", "ticker"])
    audit_df.to_csv(AUDIT_PATH, index=False)
    digest = sha256_file(SNAPSHOT_PATH)
    manifest = {
        "version": SNAPSHOT_VERSION,
        "created_at_utc": snapshot["created_at_utc"],
        "period": [SIGNAL_START, END],
        "price_start": PRICE_START,
        "broad_universe": {
            "dd60_min_pct": BROAD_DD_MIN,
            "pit_coverage_min": MIN_COVERAGE,
            "fund_threshold": None,
            "context_thresholds": None,
        },
        "snapshot_file": str(SNAPSHOT_PATH),
        "snapshot_sha256": digest,
        "snapshot_size_bytes": SNAPSHOT_PATH.stat().st_size,
        "audit_signal_rows": len(audit_df),
        "meta": snapshot["meta"],
        "purpose": "Frozen inputs for one-at-a-time robustness tests; no production change.",
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print(f"\nFrozen snapshot: {SNAPSHOT_PATH} ({SNAPSHOT_PATH.stat().st_size/1024/1024:.1f} MiB)")
    print(f"SHA256: {digest}")
    print(f"Audit signals: {len(audit_df)}")


if __name__ == "__main__":
    main()
