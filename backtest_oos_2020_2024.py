"""Frozen SIDI out-of-sample validation: 2020-01-01 .. 2024-08-10.

NO parameters are optimised in this script.
Primary candidate was frozen after the later 2024-2026 development sample:
- DD60 >= 12%, RSI<40, existing MACD/volume signal logic
- calibrated quarterly/TTM PIT proxy score >= 6.66
- minimum fundamental metric coverage 6/8
- signal at close T -> entry T+1 Open
- TP = 0.75 * ATR14
- SL = -5%
- time stop = 7 sessions
- risk = 1.5%, max 5 positions
- round-trip implementation cost = 10 bps

The current constituent universe is corrected one-sided using date_added. Removed
historical S&P members are still absent, so survivorship bias is reduced but not
eliminated. yFinance statement history can also limit true PIT coverage in early
years; this script reports that coverage instead of silently backfilling future data.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import backtest as bt
import backtest_experiments as exp
import calibrate_pit_proxy_ttm as cal_ttm
import historical_membership as membership
import pit_proxy_ttm_fast as ttm_fast
import validate_sidi_candidate as val
import yfinance_pit_proxy as pit_base
from modules.ingesta.scoring import _sector_medians, _fund_score

OOS_START = "2020-01-01"
OOS_END = "2024-08-10"
PRICE_START = "2019-09-01"  # indicator warm-up only
PROXY_THRESHOLD = 6.66
MIN_COVERAGE = 6
COST_BPS = 10

CANDIDATE = exp.Experiment(
    "OOS_PIT66_ATR075_T7",
    target_mode="atr", atr_mult=0.75,
    stop_mode="fixed", stop_pct=0.05,
    time_stop=7,
    description="Frozen candidate: PIT>=6.66, TP0.75ATR, SL5%, TS7",
)
BASELINE_EXIT = exp.Experiment(
    "OOS_PIT66_ATR15_T15",
    target_mode="atr", atr_mult=1.5,
    stop_mode="fixed", stop_pct=0.05,
    time_stop=15,
    description="Same PIT signals, old exit: TP1.5ATR, SL5%, TS15",
)


def price_maps(prices: dict[str, pd.DataFrame]) -> dict[str, dict[str, float]]:
    out = {}
    for ticker, df in prices.items():
        out[ticker] = {
            str(r.Date)[:10]: float(r.Close)
            for r in df[["Date", "Close"]].itertuples(index=False)
            if pd.notna(r.Close)
        }
    return out


def earliest_col(df) -> str | None:
    if df is None or df.empty:
        return None
    vals = []
    for c in df.columns:
        try:
            vals.append(pd.Timestamp(c).tz_localize(None))
        except Exception:
            pass
    return min(vals).date().isoformat() if vals else None


def statement_audit(cache: dict) -> dict:
    rows = []
    for ticker, s in cache.items():
        rows.append({
            "ticker": ticker,
            "earliest_quarterly": earliest_col(s.get("qinc")),
            "earliest_annual": earliest_col(s.get("inc")),
        })
    df = pd.DataFrame(rows)
    q = pd.to_datetime(df["earliest_quarterly"], errors="coerce") if not df.empty else pd.Series(dtype="datetime64[ns]")
    a = pd.to_datetime(df["earliest_annual"], errors="coerce") if not df.empty else pd.Series(dtype="datetime64[ns]")
    return {
        "tickers_with_statements": int(len(df)),
        "quarterly_earliest_median": q.median().date().isoformat() if q.notna().any() else None,
        "annual_earliest_median": a.median().date().isoformat() if a.notna().any() else None,
        "quarterly_available_by_2020_end_pct": float((q <= pd.Timestamp("2020-12-31")).mean()*100) if len(q) else 0.0,
        "quarterly_available_by_2021_end_pct": float((q <= pd.Timestamp("2021-12-31")).mean()*100) if len(q) else 0.0,
        "quarterly_available_by_2022_end_pct": float((q <= pd.Timestamp("2022-12-31")).mean()*100) if len(q) else 0.0,
        "annual_available_by_2020_end_pct": float((a <= pd.Timestamp("2020-12-31")).mean()*100) if len(a) else 0.0,
        "annual_available_by_2021_end_pct": float((a <= pd.Timestamp("2021-12-31")).mean()*100) if len(a) else 0.0,
        "annual_available_by_2022_end_pct": float((a <= pd.Timestamp("2022-12-31")).mean()*100) if len(a) else 0.0,
    }


def active_tickers(asof: str, tickers: list[str], added: dict[str, str | None], state_cache: dict) -> list[str]:
    return [
        t for t in tickers
        if t in state_cache and (added.get(t) is None or asof >= added[t])
    ]


def dynamic_scores(signal_dates, tickers, sectors, state_cache, prices_by_date, added):
    score_by_date = {}
    coverage_rows = []
    n = len(signal_dates)
    for i, asof in enumerate(signal_dates, 1):
        active = active_tickers(asof, tickers, added, state_cache)
        rows = []
        for ticker in active:
            price = prices_by_date.get(ticker, {}).get(asof, np.nan)
            rows.append(ttm_fast.row_asof(
                state_cache, ticker, sectors.get(ticker, "Unknown"), asof, price
            ))
        df = pd.DataFrame(rows)
        if df.empty:
            score_by_date[asof] = {}
            coverage_rows.append({
                "date": asof, "active": 0, "mean_coverage": 0.0,
                "coverage_ge6_pct": 0.0, "score_ge_threshold": 0,
            })
            continue
        sm = _sector_medians(df)
        scored = df.apply(lambda r: _fund_score(r, sm), axis=1)
        df = pd.concat([df, scored], axis=1)
        score_by_date[asof] = {
            str(r.ticker): {
                "fund_score": float(r.fund_score),
                "coverage": int(r.metric_coverage),
            }
            for r in df.itertuples()
        }
        coverage_rows.append({
            "date": asof,
            "active": len(active),
            "mean_coverage": float(df["metric_coverage"].mean()),
            "coverage_ge6_pct": float((df["metric_coverage"] >= MIN_COVERAGE).mean()*100),
            "score_ge_threshold": int((df["fund_score"] >= PROXY_THRESHOLD).sum()),
        })
        if i % 50 == 0 or i == n:
            c = coverage_rows[-1]
            print(
                f"  PIT OOS {i}/{n} {asof}: active={c['active']} "
                f"coverage={c['mean_coverage']:.2f}/8 >=6={c['coverage_ge6_pct']:.1f}% "
                f"score>={PROXY_THRESHOLD:.2f}: {c['score_ge_threshold']}"
            )
    return score_by_date, pd.DataFrame(coverage_rows)


def filter_signals(tech_map: dict, scores: dict):
    out = {}
    stats = {"technical": 0, "missing_score": 0, "low_coverage": 0, "below_threshold": 0, "kept": 0}
    for ticker, sigs in tech_map.items():
        for date, sig in sigs.items():
            stats["technical"] += 1
            rec = scores.get(date, {}).get(ticker)
            if rec is None:
                stats["missing_score"] += 1
                continue
            if rec["coverage"] < MIN_COVERAGE:
                stats["low_coverage"] += 1
                continue
            if rec["fund_score"] < PROXY_THRESHOLD:
                stats["below_threshold"] += 1
                continue
            item = dict(sig)
            item["pit_fund_score"] = rec["fund_score"]
            item["pit_coverage"] = rec["coverage"]
            out.setdefault(ticker, {})[date] = item
            stats["kept"] += 1
    return out, stats


def run(name, signal_map, prices, indicators, fund_scores, model, start, end):
    st, trades = val.run_sim(
        signal_map, prices, indicators, fund_scores,
        model, start, end, cost_bps=COST_BPS,
    )
    st["run_name"] = name
    return st, trades


def main():
    print("="*112)
    print("SIDI FROZEN OUT-OF-SAMPLE 2020-2024")
    print("="*112)
    print(
        f"Period={OOS_START}..{OOS_END} | PIT proxy>={PROXY_THRESHOLD} | coverage>={MIN_COVERAGE}/8 | "
        f"TP=0.75ATR | SL=5% | TS=7 | costs={COST_BPS}bps RT"
    )
    print("No parameter optimisation is performed in this run.\n")

    tickers = bt.load_tickers()
    fund_scores = bt.load_fundamental_scores()
    prices = bt.download_prices(tickers, start_date=PRICE_START, end_date=OOS_END)
    indicators = bt.build_indicators(prices)
    pmap = price_maps(prices)
    sectors = pit_base.load_sector_map()
    added = membership.load_date_added()

    tech_spec = exp.signal_variant(12.0, False)
    tech_spec["min_fund_score"] = 0.0
    tech_raw, tech_raw_n = bt.build_signal_map(
        indicators, tech_spec, spy_dict={}, date_from=OOS_START, date_to=OOS_END,
        fund_scores=fund_scores, sector_regime=None,
    )
    tech_map, member_stats = membership.filter_signal_map(tech_raw, added)
    signal_dates = sorted({d for sigs in tech_map.values() for d in sigs})
    print(
        f"Technical DD12 signals raw={tech_raw_n}; after known date_added={member_stats['kept']}; "
        f"removed pre-membership={member_stats['removed_pre_membership']}; dates={len(signal_dates)}"
    )

    statement_cache, failed = cal_ttm.fetch_full_statements(tickers)
    audit = statement_audit(statement_cache)
    print("\nSTATEMENT HISTORY AUDIT")
    print(json.dumps(audit, indent=2))
    print(f"Statement failures: {len(failed)}")

    state_cache = ttm_fast.build_state_cache(statement_cache, OOS_START, OOS_END)
    scores, coverage = dynamic_scores(
        signal_dates, tickers, sectors, state_cache, pmap, added
    )
    coverage.to_csv("sidi_oos_2020_2024_coverage.csv", index=False)
    pit_map, filter_stats = filter_signals(tech_map, scores)
    print("\nPIT FILTER")
    print(json.dumps(filter_stats, indent=2))

    summary_rows = []
    # Diagnostic technical-only candidate: tells us whether price edge existed even
    # when historical fundamental data is unavailable. It is NOT the primary model.
    for name, smap, model in [
        ("TECH_ONLY_CANDIDATE", tech_map, CANDIDATE),
        ("PIT66_CANDIDATE", pit_map, CANDIDATE),
        ("PIT66_OLD_EXIT", pit_map, BASELINE_EXIT),
    ]:
        st, _ = run(name, smap, prices, indicators, fund_scores, model, OOS_START, OOS_END)
        summary_rows.append(st)
        print(
            f"{name:<22} trades={st['trades']:4d} WR={st['win_rate']:6.2f}% "
            f"PF={st['profit_factor']:5.2f} Ret={st['total_return']:8.2f}% "
            f"CAGR={st['cagr']:6.2f}% MDD={st['max_drawdown_mtm']:7.2f}% Days={st['avg_days']:4.2f}"
        )

    year_rows = []
    years = [
        ("2020", "2020-01-01", "2020-12-31"),
        ("2021", "2021-01-01", "2021-12-31"),
        ("2022", "2022-01-01", "2022-12-31"),
        ("2023", "2023-01-01", "2023-12-31"),
        ("2024_YTD", "2024-01-01", OOS_END),
    ]
    print("\nYEAR-BY-YEAR — capital reset each year")
    print("-"*112)
    for year, start, end in years:
        for name, model in [("PIT66_CANDIDATE", CANDIDATE), ("PIT66_OLD_EXIT", BASELINE_EXIT)]:
            st, _ = run(name, pit_map, prices, indicators, fund_scores, model, start, end)
            st["year"] = year
            year_rows.append(st)
            print(
                f"{year:<8} {name:<18} trades={st['trades']:3d} WR={st['win_rate']:6.2f}% "
                f"PF={st['profit_factor']:5.2f} Ret={st['total_return']:7.2f}% "
                f"MDD={st['max_drawdown_mtm']:7.2f}% Days={st['avg_days']:4.2f}"
            )

    summary_df = pd.DataFrame(summary_rows).drop(columns=["config"], errors="ignore")
    year_df = pd.DataFrame(year_rows).drop(columns=["config"], errors="ignore")
    summary_df.to_csv("sidi_oos_2020_2024_summary.csv", index=False)
    year_df.to_csv("sidi_oos_2020_2024_by_year.csv", index=False)

    payload = {
        "generated_at": datetime.utcnow().isoformat()+"Z",
        "period": [OOS_START, OOS_END],
        "frozen_candidate": {
            "dd60_min_pct": 12.0,
            "pit_proxy_threshold": PROXY_THRESHOLD,
            "min_metric_coverage": MIN_COVERAGE,
            "entry": "next-session open",
            "target": "0.75x ATR14",
            "stop_pct": 5.0,
            "time_stop_sessions": 7,
            "risk_pct": 1.5,
            "max_positions": 5,
            "round_trip_cost_bps": COST_BPS,
        },
        "statement_history_audit": audit,
        "statement_failures": failed,
        "membership_filter": member_stats,
        "fundamental_filter": filter_stats,
        "coverage_by_year": (
            coverage.assign(year=coverage["date"].str[:4])
            .groupby("year")
            .agg(signal_dates=("date", "size"), mean_coverage=("mean_coverage", "mean"),
                 coverage_ge6_pct=("coverage_ge6_pct", "mean"), score_ge_threshold=("score_ge_threshold", "mean"))
            .reset_index().to_dict("records")
            if not coverage.empty else []
        ),
        "summaries": summary_rows,
        "by_year": year_rows,
        "limitations": [
            "Historical yFinance statements may contain later restatements.",
            "45d quarterly / 90d annual lags approximate rather than observe filing timestamps.",
            "Current constituents before date_added are excluded, but removed historical S&P members are still absent.",
            "If yFinance no longer exposes old statement columns, early-year PIT coverage can be insufficient; this is reported explicitly.",
            "Daily OHLC bars resolve same-day TP+SL ambiguity conservatively as STOP.",
        ],
    }
    Path("sidi_oos_2020_2024_results.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("\nSaved OOS summary, yearly results, coverage and JSON audit.")


if __name__ == "__main__":
    main()
