"""Calibrate the historical SIDI fundamental PIT proxy against real archived snapshots.

This does NOT backtest trading performance. It answers a narrower question:
how faithfully does the reconstructed historical fund_score reproduce the score
that SIDI actually calculated on dates for which a real snapshot exists?

Outputs:
- overall score correlation/error metrics;
- classification metrics at real threshold 6.5;
- best proxy threshold and leave-one-snapshot-out validation;
- per-date metrics;
- per-input-metric correlations/errors;
- largest persistent ticker mismatches.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

import backtest as bt
import backtest_yf_pit_proxy as base
import pit_proxy_fast as fast
import historical_membership as membership
from modules.ingesta.scoring import _sector_medians, _fund_score

REAL_THRESHOLD = 6.5
LAG_DAYS = 90
SNAP_DIR = Path("data/history/fundamentals")
MASTER_DIR = Path("data/master")
METRICS = [
    "revenue_growth", "eps_growth", "roe", "debt_equity",
    "current_ratio", "fcf_ni_ratio", "pe", "pb",
]


def snapshot_dates() -> list[str]:
    dates = []
    for p in sorted(SNAP_DIR.glob("fundamentals_*.csv")):
        stamp = p.stem.split("_")[-1]
        if len(stamp) == 8 and stamp.isdigit():
            dates.append(f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}")
    return dates


def load_master():
    files = sorted(MASTER_DIR.glob("sp500_full_export_*.csv"))
    if not files:
        raise RuntimeError("No master export found")
    df = pd.read_csv(files[-1])
    tickers = df["ticker"].dropna().astype(str).str.upper().tolist()
    sectors = dict(zip(df["ticker"].astype(str).str.upper(), df["sector"].fillna("Unknown")))
    return tickers, sectors


def download_close_maps(tickers: list[str], start: str, end: str) -> dict[str, dict[str, float]]:
    """Download only the small calibration price window in chunks."""
    out: dict[str, dict[str, float]] = {}
    chunk_size = 50
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i+chunk_size]
        raw = yf.download(chunk, start=start, end=end, auto_adjust=True,
                          progress=False, group_by="ticker", threads=False)
        if raw is None or raw.empty:
            continue
        if isinstance(raw.columns, pd.MultiIndex):
            for t in chunk:
                try:
                    df = raw[t].dropna(subset=["Close"])
                    out[t] = {str(idx)[:10]: float(r["Close"]) for idx, r in df.iterrows()}
                except Exception:
                    pass
        else:
            t = chunk[0]
            df = raw.dropna(subset=["Close"])
            out[t] = {str(idx)[:10]: float(r["Close"]) for idx, r in df.iterrows()}
    return out


def score_proxy_for_date(asof: str, tickers: list[str], sectors: dict,
                         state_cache: dict, close_maps: dict,
                         added: dict[str, str | None]) -> pd.DataFrame:
    active = [t for t in tickers if added.get(t) is None or asof >= added[t]]
    rows = []
    for t in active:
        price = base.close_on_or_before(close_maps.get(t, {}), asof)
        rows.append(fast.row_asof(state_cache, t, sectors.get(t, "Unknown"), asof, price))
    df = pd.DataFrame(rows)
    sm = _sector_medians(df)
    scores = df.apply(lambda r: _fund_score(r, sm), axis=1)
    return pd.concat([df, scores], axis=1)


def safe_corr(a: pd.Series, b: pd.Series, rank: bool = False):
    x = pd.to_numeric(a, errors="coerce")
    y = pd.to_numeric(b, errors="coerce")
    ok = x.notna() & y.notna()
    if ok.sum() < 3:
        return None
    x, y = x[ok], y[ok]
    if rank:
        x = x.rank(method="average")
        y = y.rank(method="average")
    v = x.corr(y, method="pearson")
    return None if pd.isna(v) else float(v)


def class_metrics(real: pd.Series, proxy: pd.Series, proxy_threshold: float) -> dict:
    r = pd.to_numeric(real, errors="coerce") >= REAL_THRESHOLD
    p = pd.to_numeric(proxy, errors="coerce") >= proxy_threshold
    tp = int((r & p).sum()); tn = int((~r & ~p).sum())
    fp = int((~r & p).sum()); fn = int((r & ~p).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    specificity = tn / (tn + fp) if tn + fp else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    jaccard = tp / (tp + fp + fn) if tp + fp + fn else 0.0
    bal_acc = (recall + specificity) / 2
    return {
        "threshold": float(proxy_threshold), "tp": tp, "tn": tn, "fp": fp, "fn": fn,
        "agreement": float((r == p).mean()), "precision": precision, "recall": recall,
        "specificity": specificity, "f1": f1, "jaccard": jaccard,
        "balanced_accuracy": bal_acc,
        "real_pass": int(r.sum()), "proxy_pass": int(p.sum()),
    }


def best_threshold(df: pd.DataFrame) -> dict:
    best = None
    for th in np.arange(5.00, 8.001, 0.01):
        m = class_metrics(df.real_score, df.proxy_score, float(round(th, 2)))
        # Primary: Jaccard (same selected set), tie-breakers F1 then agreement.
        key = (m["jaccard"], m["f1"], m["agreement"])
        if best is None or key > best[0]:
            best = (key, m)
    return best[1]


def loso_threshold_validation(all_pairs: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    rows = []
    dates = sorted(all_pairs["date"].unique())
    for held in dates:
        train = all_pairs[all_pairs.date != held]
        test = all_pairs[all_pairs.date == held]
        chosen = best_threshold(train)["threshold"]
        m = class_metrics(test.real_score, test.proxy_score, chosen)
        m["date"] = held
        m["train_selected_threshold"] = chosen
        rows.append(m)
    df = pd.DataFrame(rows)
    weights = all_pairs.groupby("date").size().reindex(df.date).to_numpy(dtype=float)
    weights = weights / weights.sum()
    summary = {
        "dates": int(len(df)),
        "threshold_mean": float(df.train_selected_threshold.mean()),
        "threshold_min": float(df.train_selected_threshold.min()),
        "threshold_max": float(df.train_selected_threshold.max()),
        "weighted_agreement": float(np.average(df.agreement, weights=weights)),
        "weighted_precision": float(np.average(df.precision, weights=weights)),
        "weighted_recall": float(np.average(df.recall, weights=weights)),
        "weighted_f1": float(np.average(df.f1, weights=weights)),
        "weighted_jaccard": float(np.average(df.jaccard, weights=weights)),
        "weighted_balanced_accuracy": float(np.average(df.balanced_accuracy, weights=weights)),
    }
    return df, summary


def main():
    dates = snapshot_dates()
    if not dates:
        raise SystemExit("No archived fundamental snapshots")
    print(f"Archived snapshots: {len(dates)} ({dates[0]} -> {dates[-1]})")

    tickers, sectors = load_master()
    added = membership.load_date_added()

    # Statements are downloaded once; annual PIT state is cached once.
    statement_cache, failed = base.fetch_statement_cache(tickers)
    state_cache = fast.build_state_cache(statement_cache, dates[0], dates[-1], LAG_DAYS)

    start = (pd.Timestamp(dates[0]) - pd.Timedelta(days=5)).date().isoformat()
    end = (pd.Timestamp(dates[-1]) + pd.Timedelta(days=2)).date().isoformat()
    close_maps = download_close_maps(tickers, start, end)

    pair_frames = []
    per_date = []
    for asof in dates:
        stamp = asof.replace("-", "")
        real_path = SNAP_DIR / f"fundamentals_{stamp}.csv"
        real = pd.read_csv(real_path)
        proxy = score_proxy_for_date(asof, tickers, sectors, state_cache, close_maps, added)

        keep_real = ["ticker", "fund_score"] + [c for c in METRICS if c in real.columns]
        keep_proxy = ["ticker", "fund_score"] + [c for c in METRICS if c in proxy.columns]
        rr = real[keep_real].rename(columns={"fund_score": "real_score", **{c: f"real_{c}" for c in METRICS}})
        pp = proxy[keep_proxy].rename(columns={"fund_score": "proxy_score", **{c: f"proxy_{c}" for c in METRICS}})
        comp = rr.merge(pp, on="ticker", how="inner").dropna(subset=["real_score", "proxy_score"])
        comp["date"] = asof
        pair_frames.append(comp)

        same65 = class_metrics(comp.real_score, comp.proxy_score, REAL_THRESHOLD)
        same65.update({
            "date": asof, "n": int(len(comp)),
            "pearson": safe_corr(comp.real_score, comp.proxy_score),
            "spearman": safe_corr(comp.real_score, comp.proxy_score, rank=True),
            "mae": float((comp.proxy_score - comp.real_score).abs().mean()),
            "bias": float((comp.proxy_score - comp.real_score).mean()),
        })
        per_date.append(same65)
        print(f"{asof}: n={len(comp)} pearson={same65['pearson']:.3f} spearman={same65['spearman']:.3f} "
              f"agree65={same65['agreement']:.1%} J={same65['jaccard']:.3f}")

    pairs = pd.concat(pair_frames, ignore_index=True)
    per_date_df = pd.DataFrame(per_date)

    diff = pairs.proxy_score - pairs.real_score
    baseline = class_metrics(pairs.real_score, pairs.proxy_score, REAL_THRESHOLD)
    optimum = best_threshold(pairs)
    loso_df, loso = loso_threshold_validation(pairs)

    # Linear diagnostic only; we do not automatically change production score formula.
    slope, intercept = np.polyfit(pairs.proxy_score.to_numpy(), pairs.real_score.to_numpy(), 1)
    equivalent_proxy_for_real65 = float((REAL_THRESHOLD - intercept) / slope) if slope != 0 else None

    metric_rows = []
    for m in METRICS:
        rc, pc = f"real_{m}", f"proxy_{m}"
        if rc not in pairs or pc not in pairs:
            continue
        x = pd.to_numeric(pairs[rc], errors="coerce")
        y = pd.to_numeric(pairs[pc], errors="coerce")
        ok = x.notna() & y.notna() & np.isfinite(x) & np.isfinite(y)
        if ok.sum() < 3:
            continue
        metric_rows.append({
            "metric": m, "n": int(ok.sum()),
            "pearson": safe_corr(x[ok], y[ok]),
            "spearman": safe_corr(x[ok], y[ok], rank=True),
            "mae": float((y[ok] - x[ok]).abs().mean()),
            "median_abs_error": float((y[ok] - x[ok]).abs().median()),
        })
    metric_df = pd.DataFrame(metric_rows)

    ticker_err = (pairs.assign(abs_error=diff.abs())
                  .groupby("ticker")
                  .agg(n=("real_score", "size"), real_mean=("real_score", "mean"),
                       proxy_mean=("proxy_score", "mean"), mae=("abs_error", "mean"))
                  .sort_values("mae", ascending=False).reset_index())

    summary = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "snapshots": len(dates), "pairs": int(len(pairs)),
        "date_range": [dates[0], dates[-1]],
        "statement_failures": failed,
        "score": {
            "pearson": safe_corr(pairs.real_score, pairs.proxy_score),
            "spearman": safe_corr(pairs.real_score, pairs.proxy_score, rank=True),
            "mae": float(diff.abs().mean()),
            "rmse": float(math.sqrt(np.mean(np.square(diff)))),
            "bias_proxy_minus_real": float(diff.mean()),
            "median_error": float(diff.median()),
        },
        "threshold_6_5_unadjusted": baseline,
        "best_in_sample_proxy_threshold": optimum,
        "leave_one_snapshot_out": loso,
        "linear_diagnostic": {
            "real_equals_intercept_plus_slope_proxy": {"intercept": float(intercept), "slope": float(slope)},
            "proxy_threshold_equivalent_to_real_6_5": equivalent_proxy_for_real65,
        },
        "caveats": [
            "Archived dates are clustered in Aug-Sep 2026; they validate score reconstruction fidelity, not 2024-2025 restatement risk.",
            "Proxy uses a conservative 90-day lag rather than exact filing dates.",
            "Current constituents removed before today are still absent from the reconstructed universe.",
        ],
    }

    Path("sidi_pit_calibration_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    per_date_df.to_csv("sidi_pit_calibration_by_date.csv", index=False)
    metric_df.to_csv("sidi_pit_calibration_by_metric.csv", index=False)
    loso_df.to_csv("sidi_pit_calibration_loso.csv", index=False)
    ticker_err.head(100).to_csv("sidi_pit_calibration_ticker_mismatches.csv", index=False)

    print("\n=== CALIBRATION SUMMARY ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
