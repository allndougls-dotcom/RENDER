"""Calibrate yFinance PIT_PROXY scores against a real archived SIDI snapshot.

The Git-history backfill recovered actual daily fundamental scores from late
Aug/Sep 2026. This script reconstructs the same date using the conservative
90-day annual proxy and measures how well it preserves SIDI's score ranking and
>=6.5 classification.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

import backtest as bt
import backtest_yf_pit_proxy as full
import yfinance_pit_proxy as pit
from modules.ingesta.scoring import _sector_medians, _fund_score


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default="2026-09-10")
    p.add_argument("--threshold", type=float, default=6.5)
    args = p.parse_args()
    stamp = args.date.replace("-", "")
    archived_path = Path(f"data/history/fundamentals/fundamentals_{stamp}.csv")
    if not archived_path.exists():
        raise SystemExit(f"Missing archived snapshot: {archived_path}")

    real = pd.read_csv(archived_path)
    real["ticker"] = real["ticker"].astype(str).str.upper()
    tickers = real["ticker"].tolist()
    sectors = {str(r.ticker).upper(): str(r.sector) for r in real.itertuples()}

    print(f"Calibration date={args.date}; real snapshot rows={len(real)}")
    statements, failed = full.fetch_statement_cache(tickers)

    # Price history only needs a short band around calibration date.
    prices = bt.download_prices(tickers, start_date=(pd.Timestamp(args.date)-pd.Timedelta(days=15)).date().isoformat(), end_date=args.date)
    pmap = full.price_maps(prices)

    rows = [
        full.metrics_from_cache(t, sectors.get(t, "Unknown"), args.date, statements, pmap)
        for t in tickers
    ]
    df = pd.DataFrame(rows)
    sm = _sector_medians(df)
    scores = df.apply(lambda r: _fund_score(r, sm), axis=1)
    proxy = pd.concat([df, scores], axis=1)[["ticker", "fund_score", "metric_coverage"]].rename(
        columns={"fund_score": "proxy_score"}
    )

    comp = real[["ticker", "fund_score"]].rename(columns={"fund_score": "real_score"}).merge(proxy, on="ticker", how="inner")
    comp = comp.dropna(subset=["real_score", "proxy_score"])
    comp["abs_error"] = (comp["proxy_score"] - comp["real_score"]).abs()
    comp["real_pass"] = comp["real_score"] >= args.threshold
    comp["proxy_pass"] = comp["proxy_score"] >= args.threshold

    pearson = comp["real_score"].corr(comp["proxy_score"], method="pearson")
    spearman = comp["real_score"].corr(comp["proxy_score"], method="spearman")
    mae = comp["abs_error"].mean()
    agreement = (comp["real_pass"] == comp["proxy_pass"]).mean()
    tp = int((comp["real_pass"] & comp["proxy_pass"]).sum())
    fp = int((~comp["real_pass"] & comp["proxy_pass"]).sum())
    fn = int((comp["real_pass"] & ~comp["proxy_pass"]).sum())
    precision = tp/(tp+fp) if tp+fp else np.nan
    recall = tp/(tp+fn) if tp+fn else np.nan
    jaccard = tp/(tp+fp+fn) if tp+fp+fn else np.nan

    summary = pd.DataFrame([{
        "date": args.date,
        "n": len(comp),
        "failed_statement_tickers": len(failed),
        "mean_metric_coverage": comp["metric_coverage"].mean(),
        "pearson": pearson,
        "spearman": spearman,
        "mae_score_points": mae,
        "threshold": args.threshold,
        "real_pass_count": int(comp["real_pass"].sum()),
        "proxy_pass_count": int(comp["proxy_pass"].sum()),
        "classification_agreement": agreement,
        "precision_proxy_vs_real": precision,
        "recall_proxy_vs_real": recall,
        "jaccard_pass_set": jaccard,
    }])

    print(summary.to_string(index=False))
    summary.to_csv("sidi_yf_pit_proxy_calibration_summary.csv", index=False)
    comp.sort_values("abs_error", ascending=False).to_csv("sidi_yf_pit_proxy_calibration_detail.csv", index=False)
    print("Saved calibration summary/detail")


if __name__ == "__main__":
    main()
