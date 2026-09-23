"""SIDI PIT_PROXY v2: membership-aware + archived-score calibration.

Builds on backtest_yf_pit_proxy.py but adds:
1) one-sided historical S&P membership correction using `date_added`;
2) sector medians computed only from current constituents that had already joined;
3) calibration of reconstructed scores against a real archived SIDI snapshot.

Still not PIT_TRUE because removed historical constituents are absent and
historical yFinance statements may contain later restatements.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import backtest as bt
import backtest_experiments as exp
import backtest_yf_pit_proxy as base
import backtest_yf_pit_proxy_fast as fast
import historical_membership as membership
import validate_sidi_candidate as val
from modules.ingesta.scoring import _sector_medians, _fund_score

START = base.START
END = base.END
COST_BPS = base.COST_BPS
MIN_SCORE = base.MIN_SCORE
MIN_COVERAGE = base.MIN_COVERAGE
CALIBRATION_DATE = "2026-09-09"


def active_tickers(asof: str, tickers: list[str], added: dict[str, str | None]) -> list[str]:
    return [t for t in tickers if added.get(t) is None or asof >= added[t]]


def dynamic_scores_membership(signal_dates, tickers, sectors, state_cache, prices_by_date, added):
    score_by_date = {}
    coverage_rows = []
    n = len(signal_dates)
    for i, asof in enumerate(signal_dates, 1):
        active = active_tickers(asof, tickers, added)
        rows = []
        for ticker in active:
            price = base.close_on_or_before(prices_by_date.get(ticker, {}), asof)
            rows.append(
                fast.row_asof(state_cache, ticker, sectors.get(ticker, "Unknown"), asof, price)
            )
        df = pd.DataFrame(rows)
        sm = _sector_medians(df)
        scores = df.apply(lambda r: _fund_score(r, sm), axis=1)
        df = pd.concat([df, scores], axis=1)
        score_by_date[asof] = {
            str(r.ticker): {"fund_score": float(r.fund_score), "coverage": int(r.metric_coverage)}
            for r in df.itertuples()
        }
        coverage_rows.append({
            "date": asof,
            "active_current_constituents": len(active),
            "mean_coverage": float(df["metric_coverage"].mean()),
            "coverage_ge6_pct": float((df["metric_coverage"] >= MIN_COVERAGE).mean() * 100),
            "score_ge65": int((df["fund_score"] >= MIN_SCORE).sum()),
            "score_median": float(df["fund_score"].median()),
        })
        if i % 25 == 0 or i == n:
            c = coverage_rows[-1]
            print(
                f"  PITv2 {i}/{n} {asof}: active={len(active)} coverage={c['mean_coverage']:.2f}/8 "
                f"score>=6.5={c['score_ge65']}"
            )
    return score_by_date, pd.DataFrame(coverage_rows)


def calibrate(score_by_date: dict, date: str, threshold: float = 6.5) -> dict:
    stamp = date.replace("-", "")
    path = Path(f"data/history/fundamentals/fundamentals_{stamp}.csv")
    if not path.exists() or date not in score_by_date:
        return {"date": date, "available": False}
    real = pd.read_csv(path)[["ticker", "fund_score"]].rename(columns={"fund_score": "real_score"})
    proxy_rows = [
        {"ticker": t, "proxy_score": rec["fund_score"], "coverage": rec["coverage"]}
        for t, rec in score_by_date[date].items()
    ]
    proxy = pd.DataFrame(proxy_rows)
    comp = real.merge(proxy, on="ticker", how="inner").dropna(subset=["real_score", "proxy_score"])
    comp["real_pass"] = comp["real_score"] >= threshold
    comp["proxy_pass"] = comp["proxy_score"] >= threshold
    tp = int((comp.real_pass & comp.proxy_pass).sum())
    fp = int((~comp.real_pass & comp.proxy_pass).sum())
    fn = int((comp.real_pass & ~comp.proxy_pass).sum())
    # Spearman without scipy: Pearson correlation of ranked values.
    real_rank = comp["real_score"].rank(method="average")
    proxy_rank = comp["proxy_score"].rank(method="average")
    return {
        "date": date,
        "available": True,
        "n": int(len(comp)),
        "pearson": float(comp.real_score.corr(comp.proxy_score, method="pearson")),
        "spearman": float(real_rank.corr(proxy_rank, method="pearson")),
        "mae": float((comp.proxy_score - comp.real_score).abs().mean()),
        "classification_agreement": float((comp.real_pass == comp.proxy_pass).mean()),
        "real_pass": int(comp.real_pass.sum()),
        "proxy_pass": int(comp.proxy_pass.sum()),
        "precision": float(tp / (tp + fp)) if tp + fp else None,
        "recall": float(tp / (tp + fn)) if tp + fn else None,
        "jaccard": float(tp / (tp + fp + fn)) if tp + fp + fn else None,
    }


def main():
    print("=" * 110)
    print("SIDI PIT_PROXY v2 — DYNAMIC FUNDAMENTALS + MEMBERSHIP + REAL-SNAPSHOT CALIBRATION")
    print("=" * 110)

    tickers = bt.load_tickers()
    fund_scores = bt.load_fundamental_scores()
    prices = bt.download_prices(tickers, start_date=START, end_date=END)
    indicators = bt.build_indicators(prices)
    prices_by_date = base.price_maps(prices)
    sectors = base.pit.load_sector_map()
    added = membership.load_date_added()

    tech_spec = exp.signal_variant(12.0, False)
    tech_spec["min_fund_score"] = 0.0
    tech_raw, tech_n_raw = bt.build_signal_map(
        indicators, tech_spec, spy_dict={}, date_from=START, date_to=END,
        fund_scores=fund_scores, sector_regime=None,
    )
    tech_map, member_stats = membership.filter_signal_map(tech_raw, added)
    tech_n = member_stats["kept"]
    print(f"Technical signals raw={tech_n_raw}; membership-kept={tech_n}; removed pre-membership={member_stats['removed_pre_membership']}")

    current_raw, current_n_raw = base.current_proxy_signal_map(indicators, fund_scores)
    current_map, current_member_stats = membership.filter_signal_map(current_raw, added)
    current_n = current_member_stats["kept"]

    signal_dates = sorted({d for sigs in tech_map.values() for d in sigs})
    scoring_dates = sorted(set(signal_dates) | {CALIBRATION_DATE})

    statement_cache, failed = base.fetch_statement_cache(tickers)
    print(f"Statement cache {len(statement_cache)}/{len(tickers)}; failed={len(failed)}")
    state_cache = fast.build_annual_state_cache(statement_cache)
    print(f"Annual PIT state cache built: {len(state_cache)} tickers")
    score_by_date, coverage = dynamic_scores_membership(
        scoring_dates, tickers, sectors, state_cache, prices_by_date, added
    )
    coverage.to_csv("sidi_yf_pit_proxy_v2_coverage.csv", index=False)

    pit_map, pit_n = base.filter_signal_map(tech_map, score_by_date, require_coverage=False)
    pit_cov_map, pit_cov_n = base.filter_signal_map(tech_map, score_by_date, require_coverage=True)

    variants = [
        ("TECH_NO_FUND_MEMBERSHIP", tech_map, tech_n),
        ("CURRENT_PROXY65_MEMBERSHIP", current_map, current_n),
        ("YF_PIT65_MEMBERSHIP", pit_map, pit_n),
        ("YF_PIT65_COV6_MEMBERSHIP", pit_cov_map, pit_cov_n),
    ]
    summaries = []
    print("\nRESULTS")
    print("-" * 110)
    for name, smap, n in variants:
        st, trades = base.run_variant(name, smap, prices, indicators, fund_scores)
        st["builder_signals"] = n
        summaries.append(st)
        print(
            f"{name:<30} sig={n:5d} trades={st['trades']:4d} WR={st['win_rate']:6.2f}% "
            f"PF={st['profit_factor']:5.2f} Ret={st['total_return']:8.2f}% "
            f"MDD={st['max_drawdown_mtm']:7.2f}% Days={st['avg_days']:4.2f}"
        )

    calibration = calibrate(score_by_date, CALIBRATION_DATE, MIN_SCORE)
    print("\nCALIBRATION VS REAL ARCHIVED SIDI SCORE")
    print(json.dumps(calibration, indent=2))

    pd.DataFrame(summaries).drop(columns=["config"], errors="ignore").to_csv(
        "sidi_yf_pit_proxy_v2_summary.csv", index=False
    )
    Path("sidi_yf_pit_proxy_v2_results.json").write_text(json.dumps({
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "membership": {
            "technical": member_stats,
            "current_proxy": current_member_stats,
            "limitation": "current members before date_added excluded; removed historical members still absent",
        },
        "calibration": calibration,
        "failed_statement_tickers": failed,
        "summaries": summaries,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Saved PIT_PROXY v2 results")


if __name__ == "__main__":
    main()
