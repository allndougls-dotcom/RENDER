"""Build SIDI_ABLATION_SNAPSHOT_V2 from the frozen validation V1 inputs.

Purpose
-------
A valid ablation of RSI/DD/MACD/volume cannot be performed on V1 because V1
already contains only signals that passed those technical conditions (and DD>=10).
This builder therefore creates a broader *union* containing every ticker-date
that would qualify under the frozen technical stack OR under any one-at-a-time
removal of one technical condition.

Reproducibility rules
---------------------
* Stock prices/indicators/date-added/sectors are reused byte-for-byte from
  SIDI_VALIDATION_SNAPSHOT_V1.
* For ticker-dates already present in V1, the frozen V1 PIT score/coverage and
  context values are reused exactly.
* Only genuinely new ticker-dates created by technical ablation receive newly
  reconstructed PIT/context values.
* All candidates still require PIT coverage >=6/8 and complete SPY20/abnormal20
  observations. Removing a filter means ignoring its threshold, not changing the
  data-availability population.

No production code or trading rule is changed.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import pickle
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import historical_membership as membership
import calibrate_pit_proxy_ttm as cal_ttm
import pit_proxy_ttm_fast as ttm_fast
import analyze_signal_context_features as feat
import backtest_context_filters as ctxmod
import backtest_context_filters_pit_oos as pit

V1_PATH = Path("sidi_validation_snapshot_v1.pkl.gz")
V2_PATH = Path("sidi_ablation_snapshot_v2.pkl.gz")
MANIFEST_PATH = Path("sidi_ablation_snapshot_v2_manifest.json")
AUDIT_PATH = Path("sidi_ablation_snapshot_v2_signals.csv")
VERSION = "SIDI_ABLATION_SNAPSHOT_V2"
SIGNAL_START = "2023-01-01"
END = "2026-09-10"
MIN_COVERAGE = 6
MAX_HOLD_BUFFER = 15  # match V1 signal-generation boundary


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def technical_union(indicators):
    """Union of baseline and each one-at-a-time technical ablation.

    Baseline flags are DD60<=-12, RSI<40, MACD histogram improving and
    declining volume. A row is relevant to a one-at-a-time ablation iff at
    least 3 of these 4 baseline flags are true.
    """
    out = {}
    n = 0
    for ticker, ind in indicators.items():
        sigs = {}
        length = len(ind["rsi"])
        for i in range(62, max(62, length - MAX_HOLD_BUFFER - 1)):
            dte = ind["dates"][i]
            if dte < SIGNAL_START or dte >= END:
                continue
            rsi = ind["rsi"][i]
            dd = ind["dd60"][i]
            h = ind["hist"][i]
            hp = ind["hist"][i - 1]
            atr = ind["atr"][i]
            if np.isnan(rsi) or np.isnan(dd) or np.isnan(h) or np.isnan(hp):
                continue
            rsi_ok = bool(float(rsi) < 40.0)
            dd_ok = bool(float(dd) <= -12.0)
            macd_ok = bool(float(h) > float(hp))
            vol_ok = bool(ind["vdec"][i])
            flags = int(rsi_ok) + int(dd_ok) + int(macd_ok) + int(vol_ok)
            if flags < 3:
                continue
            close = float(ind["close"][i])
            atr_exact = float(atr) if not np.isnan(atr) else 0.0
            sigs[dte] = {
                "row_idx": int(i),
                "entry": close,
                "rsi": float(rsi),
                "dd": float(dd),
                "hist": float(h),
                "hist_prev": float(hp),
                "macd_improving": macd_ok,
                "volume_decreasing": vol_ok,
                "rsi_ok": rsi_ok,
                "dd12_ok": dd_ok,
                "atr_exact": atr_exact,
                "atr_val": round(atr_exact, 4),
            }
            n += 1
        if sigs:
            out[ticker] = sigs
    return out, n


def main():
    print("=" * 126)
    print(f"BUILD {VERSION}")
    print("=" * 126)
    if not V1_PATH.exists():
        raise FileNotFoundError(f"Missing frozen V1 snapshot: {V1_PATH}")

    with gzip.open(V1_PATH, "rb") as f:
        v1 = pickle.load(f)
    if v1.get("version") != "SIDI_VALIDATION_SNAPSHOT_V1":
        raise RuntimeError(f"Unexpected V1 version: {v1.get('version')}")

    prices = v1["prices"]
    indicators = v1["indicators"]
    sectors = v1["sectors"]
    added = v1["date_added"]
    current_fund_scores = v1["current_fund_scores"]
    tickers = sorted(prices)

    raw, raw_n = technical_union(indicators)
    union_map, member_stats = membership.filter_signal_map(raw, added)
    union_n = sum(len(v) for v in union_map.values())
    signal_dates = sorted({d for sigs in union_map.values() for d in sigs})
    print(
        f"Technical union raw={raw_n}; membership-kept={union_n}; "
        f"removed={member_stats.get('removed_pre_membership', 0)}; dates={len(signal_dates)}"
    )

    # Build PIT only to fill genuinely new ticker-dates. Frozen V1 records are
    # reused later, which keeps the baseline exactly anchored to V1.
    print("Building PIT fundamentals for ablation-union dates...")
    statements, statement_failures = cal_ttm.fetch_full_statements(tickers)
    state_cache = ttm_fast.build_state_cache(statements, SIGNAL_START, END)
    pmap = pit.price_maps(prices)
    scores, coverage = pit.dynamic_scores(signal_dates, tickers, sectors, state_cache, pmap, added)

    eligible = {}
    reused_pit = 0
    new_pit = 0
    missing_score = 0
    low_coverage = 0
    for ticker, sigs in union_map.items():
        for dte, sig in sigs.items():
            frozen = v1.get("signals", {}).get(ticker, {}).get(dte)
            if frozen is not None:
                fund = frozen.get("pit_fund_score")
                cov = frozen.get("pit_coverage")
                source = "V1_FROZEN"
                reused_pit += 1
            else:
                rec = scores.get(dte, {}).get(ticker)
                if rec is None:
                    missing_score += 1
                    continue
                fund = rec.get("fund_score")
                cov = rec.get("coverage", 0)
                source = "V2_NEW"
                new_pit += 1
            if fund is None or not np.isfinite(float(fund)):
                missing_score += 1
                continue
            if int(cov or 0) < MIN_COVERAGE:
                low_coverage += 1
                continue
            item = dict(sig)
            item["pit_fund_score"] = float(fund)
            item["pit_coverage"] = int(cov)
            item["pit_source"] = source
            eligible.setdefault(ticker, {})[dte] = item

    print(
        f"Coverage-complete union={sum(len(v) for v in eligible.values())}; "
        f"PIT reused V1={reused_pit}; PIT new={new_pit}; "
        f"missing={missing_score}; low_coverage={low_coverage}"
    )

    # Context for broad union. Then restore frozen V1 context wherever available.
    prices_idx = {t: feat.to_indexed(df) for t, df in prices.items()}
    feat.DOWNLOAD_START = v1.get("price_start", "2022-01-01")
    refs = {
        s: feat.download_reference(s)
        for s in sorted(set(feat.SECTOR_ETF.values()) | {"SPY", "^VIX"})
    }
    market_breadth, sector_breadth = feat.breadth_maps(prices_idx, sectors, added)
    print("Computing context for ablation union...")
    context_new = ctxmod.build_context(eligible, prices_idx, sectors, refs, market_breadth, sector_breadth)
    context = dict(context_new)
    reused_context = 0
    for ticker, sigs in eligible.items():
        for dte in sigs:
            key = (ticker, dte)
            old = v1.get("context", {}).get(key)
            if old is not None:
                context[key] = old
                reused_context += 1

    audit = []
    missing_context = 0
    for ticker, sigs in eligible.items():
        for dte, sig in sigs.items():
            c = context.get((ticker, dte), {})
            spy20 = c.get("spy20")
            abnormal20 = c.get("abnormal20")
            complete_ctx = pd.notna(spy20) and pd.notna(abnormal20)
            if not complete_ctx:
                missing_context += 1
            audit.append({
                "ticker": ticker,
                "signal_date": dte,
                "rsi": sig["rsi"],
                "dd": sig["dd"],
                "macd_improving": sig["macd_improving"],
                "volume_decreasing": sig["volume_decreasing"],
                "pit_fund_score": sig["pit_fund_score"],
                "pit_coverage": sig["pit_coverage"],
                "pit_source": sig["pit_source"],
                "spy20": spy20,
                "abnormal20": abnormal20,
                "complete_context": complete_ctx,
            })

    snapshot = {
        "version": VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "parent_version": v1["version"],
        "parent_created_at_utc": v1.get("created_at_utc"),
        "signal_start": SIGNAL_START,
        "end": END,
        "price_start": v1.get("price_start", "2022-01-01"),
        "min_coverage": MIN_COVERAGE,
        "prices": prices,
        "indicators": indicators,
        "current_fund_scores": current_fund_scores,
        "signals": eligible,
        "context": context,
        "sectors": sectors,
        "date_added": added,
        "meta": {
            "technical_union_raw": raw_n,
            "technical_union_membership_kept": union_n,
            "membership": member_stats,
            "eligible_after_pit_coverage": sum(len(v) for v in eligible.values()),
            "pit_reused_v1": reused_pit,
            "pit_new": new_pit,
            "missing_score": missing_score,
            "low_coverage": low_coverage,
            "context_reused_v1": reused_context,
            "missing_complete_context": missing_context,
            "statement_failures": statement_failures,
        },
    }

    with gzip.open(V2_PATH, "wb", compresslevel=6) as f:
        pickle.dump(snapshot, f, protocol=pickle.HIGHEST_PROTOCOL)
    audit_df = pd.DataFrame(audit).sort_values(["signal_date", "ticker"])
    audit_df.to_csv(AUDIT_PATH, index=False)
    digest = sha256_file(V2_PATH)
    manifest = {
        "version": VERSION,
        "created_at_utc": snapshot["created_at_utc"],
        "parent_version": v1["version"],
        "period": [SIGNAL_START, END],
        "snapshot_file": str(V2_PATH),
        "snapshot_sha256": digest,
        "snapshot_size_bytes": V2_PATH.stat().st_size,
        "audit_rows": len(audit_df),
        "meta": snapshot["meta"],
        "design": "Union of baseline + each one-at-a-time technical ablation; V1 inputs reused wherever possible.",
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"Frozen ablation snapshot: {V2_PATH} ({V2_PATH.stat().st_size/1024/1024:.1f} MiB)")
    print(f"SHA256: {digest}")
    print(f"Audit rows: {len(audit_df)} | missing complete context: {missing_context}")


if __name__ == "__main__":
    main()
