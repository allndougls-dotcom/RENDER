"""SIDI survivorship-bias sensitivity test using the full AVAILABLE historical S&P 500 universe.

Frozen strategy parameters are NOT optimised here.

This run compares, over 2020-01-01 .. 2024-08-10:
1) CURRENT_ONE_SIDED: the previous current-constituent universe corrected only
   with date_added (the old partial survivorship correction).
2) FULL_AVAILABLE: point-in-time historical S&P membership snapshots, including
   removed constituents whenever historical Yahoo prices remain available.

Both comparisons are run as:
- TECH_ONLY: technical signal only, to isolate universe/price survivorship effect.
- PIT66: calibrated quarterly/TTM PIT proxy >= 6.66 with coverage >= 6/8.

Frozen execution:
- DD60 >= 12%, RSI < 40, MACD histogram improving, declining volume.
- Signal at close T -> entry next session Open.
- TP = 0.75 x ATR14; fixed SL = -5%; time stop = 7 sessions.
- Risk = 1.5% of realised capital; max 5 simultaneous positions.
- Round-trip implementation cost = 10 bps of position notional.
- Same-day TP+SL ambiguity resolves conservatively as STOP.

IMPORTANT: this is still not a fully survivorship-free backtest because Yahoo
no longer serves price history for a subset of removed/delisted constituents.
The script measures and reports that missing coverage explicitly.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd

import backtest as bt
import backtest_experiments as exp
import calibrate_pit_proxy_ttm as cal_ttm
import historical_membership as partial_membership
from historical_ticker_aliases import clean_symbol
import pit_proxy_ttm_fast as ttm_fast
import validate_sidi_candidate as val
import yfinance_pit_proxy as pit_base
from modules.ingesta.scoring import _sector_medians, _fund_score

START = "2020-01-01"
END = "2024-08-10"
PRICE_START = "2019-09-01"  # indicator warm-up
PROXY_THRESHOLD = 6.66
MIN_COVERAGE = 6
COST_BPS = 10

HANS_COMMIT = "a91ef88fad5ace83bed1f3452f451247295bcd18"
HANS_URL = (
    "https://raw.githubusercontent.com/hanshof/sp500_constituents/"
    f"{HANS_COMMIT}/sp_500_historical_components.csv"
)
LAW_COMMIT = "2e59b86998a119d68e377f9f98aa7a816cfc7d5b"
LAW_URL = (
    "https://raw.githubusercontent.com/lawcal/sp500-components-history/"
    f"{LAW_COMMIT}/data/components_history.csv"
)

CANDIDATE = exp.Experiment(
    "PIT66_ATR075_T7_FULL_AVAILABLE",
    target_mode="atr",
    atr_mult=0.75,
    stop_mode="fixed",
    stop_pct=0.05,
    time_stop=7,
    description="Frozen candidate: DD12, TP0.75ATR, SL5%, TS7",
)


def parse_members(raw) -> set[str]:
    if raw is None or pd.isna(raw):
        return set()
    return {
        clean_symbol(x)
        for x in str(raw).split(",")
        if str(x).strip()
    }


def load_membership_snapshots():
    df = pd.read_csv(HANS_URL)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = (
        df.dropna(subset=["date", "tickers"])
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )
    before = df[df["date"] <= pd.Timestamp(START)]
    if before.empty:
        raise ValueError("Historical membership source has no opening snapshot")
    opening_i = int(before.index[-1])
    use = df.iloc[opening_i:].copy()
    use = use[use["date"] <= pd.Timestamp(END)].copy()
    use["members"] = use["tickers"].map(parse_members)
    dates = [d.date().isoformat() for d in use["date"]]
    sets = use["members"].tolist()
    universe = set().union(*sets) if sets else set()
    return dates, sets, universe


def membership_at(asof: str, snapshot_dates: list[str], snapshot_sets: list[set[str]]) -> set[str]:
    i = bisect_right(snapshot_dates, asof) - 1
    return snapshot_sets[i] if i >= 0 else set()


def filter_exact_membership(signal_map, snapshot_dates, snapshot_sets):
    out = {}
    kept = removed = 0
    for ticker, sigs in signal_map.items():
        for date, sig in sigs.items():
            if ticker not in membership_at(date, snapshot_dates, snapshot_sets):
                removed += 1
                continue
            out.setdefault(ticker, {})[date] = sig
            kept += 1
    return out, {"kept": kept, "removed_not_member_on_signal_date": removed}


def clean_date_series(s):
    return pd.to_datetime(
        pd.Series(s).astype(str).str.replace("*", "", regex=False),
        errors="coerce",
    )


def load_historical_sector_map(universe: set[str]) -> dict[str, str]:
    df = pd.read_csv(LAW_URL, dtype={"cik": str})
    df["symbol_raw"] = (
        df["symbol"].astype(str).str.strip().str.upper().str.replace(".", "-", regex=False)
    )
    df["ticker"] = df["symbol_raw"].map(clean_symbol)
    df["date_added_clean"] = clean_date_series(df["date_added"])
    df["date_removed_clean"] = clean_date_series(df["date_removed"])
    overlap = (
        df["date_added_clean"].fillna(pd.Timestamp.min) <= pd.Timestamp(END)
    ) & (
        df["date_removed_clean"].fillna(pd.Timestamp.max) >= pd.Timestamp(START)
    )
    hist = df[overlap & df["ticker"].isin(universe)].copy()
    hist["has_sector"] = hist["sector"].notna() & (hist["sector"].astype(str).str.len() > 0)
    hist = hist.sort_values(
        ["ticker", "has_sector", "date_added_clean"],
        ascending=[True, False, False],
    )
    best = hist.drop_duplicates("ticker", keep="first")
    return {
        str(r.ticker): str(r.sector)
        for r in best.itertuples(index=False)
        if pd.notna(r.sector) and str(r.sector).strip()
    }


def price_maps(prices: dict[str, pd.DataFrame]) -> dict[str, dict[str, float]]:
    out = {}
    for ticker, df in prices.items():
        out[ticker] = {
            str(r.Date)[:10]: float(r.Close)
            for r in df[["Date", "Close"]].itertuples(index=False)
            if pd.notna(r.Close)
        }
    return out


def statement_audit(cache: dict, universe: set[str]) -> dict:
    available = set(cache)
    return {
        "requested": len(universe),
        "with_statements": len(universe & available),
        "missing_statements": len(universe - available),
        "coverage_pct": round(100.0 * len(universe & available) / len(universe), 2) if universe else 0.0,
    }


def dynamic_scores(signal_dates, active_fn, sectors, state_cache, prices_by_date, label):
    score_by_date = {}
    coverage_rows = []
    n = len(signal_dates)
    for i, asof in enumerate(signal_dates, 1):
        active = [t for t in active_fn(asof) if t in state_cache]
        rows = []
        for ticker in active:
            price = prices_by_date.get(ticker, {}).get(asof, np.nan)
            rows.append(
                ttm_fast.row_asof(
                    state_cache,
                    ticker,
                    sectors.get(ticker, "Unknown"),
                    asof,
                    price,
                )
            )
        df = pd.DataFrame(rows)
        if df.empty:
            score_by_date[asof] = {}
            coverage_rows.append({
                "date": asof,
                "model": label,
                "active_with_statements": 0,
                "mean_coverage": 0.0,
                "coverage_ge6_pct": 0.0,
                "score_ge_threshold": 0,
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
            "model": label,
            "active_with_statements": len(active),
            "mean_coverage": float(df["metric_coverage"].mean()),
            "coverage_ge6_pct": float((df["metric_coverage"] >= MIN_COVERAGE).mean() * 100),
            "score_ge_threshold": int((df["fund_score"] >= PROXY_THRESHOLD).sum()),
        })
        if i % 50 == 0 or i == n:
            c = coverage_rows[-1]
            print(
                f"  {label} PIT {i}/{n} {asof}: active={c['active_with_statements']} "
                f"coverage={c['mean_coverage']:.2f}/8 >=6={c['coverage_ge6_pct']:.1f}% "
                f"score>={PROXY_THRESHOLD:.2f}: {c['score_ge_threshold']}"
            )
    return score_by_date, pd.DataFrame(coverage_rows)


def filter_pit(signal_map: dict, scores: dict):
    out = {}
    stats = {
        "technical": 0,
        "missing_score": 0,
        "low_coverage": 0,
        "below_threshold": 0,
        "kept": 0,
    }
    for ticker, sigs in signal_map.items():
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


def run(name, signal_map, prices, indicators, static_fund_scores, start, end):
    st, trades = val.run_sim(
        signal_map,
        prices,
        indicators,
        static_fund_scores,
        CANDIDATE,
        start,
        end,
        cost_bps=COST_BPS,
    )
    st["run_name"] = name
    for tr in trades:
        sig = signal_map.get(tr["ticker"], {}).get(tr["signal_date"], {})
        if "pit_fund_score" in sig:
            tr["fund_score"] = round(float(sig["pit_fund_score"]), 3)
            tr["pit_coverage"] = int(sig.get("pit_coverage", 0))
    return st, trades


def annual_membership_coverage(snapshot_dates, snapshot_sets, price_tickers):
    rows = []
    for date in ["2020-01-02", "2021-01-04", "2022-01-03", "2023-01-03", "2024-01-02", END]:
        mem = membership_at(date, snapshot_dates, snapshot_sets)
        have = mem & price_tickers
        rows.append({
            "date": date,
            "members": len(mem),
            "price_available": len(have),
            "price_missing": len(mem - price_tickers),
            "price_coverage_pct": round(100.0 * len(have) / len(mem), 2) if mem else 0.0,
        })
    return pd.DataFrame(rows)


def delta_row(current: dict, full: dict) -> dict:
    return {
        "trades_delta": int(full["trades"] - current["trades"]),
        "win_rate_delta_pp": round(full["win_rate"] - current["win_rate"], 2),
        "profit_factor_delta": round(full["profit_factor"] - current["profit_factor"], 3),
        "return_delta_pp": round(full["total_return"] - current["total_return"], 2),
        "mdd_delta_pp": round(full["max_drawdown_mtm"] - current["max_drawdown_mtm"], 2),
        "avg_days_delta": round(full["avg_days"] - current["avg_days"], 2),
    }


def main():
    print("=" * 118)
    print("SIDI — FULL-AVAILABLE HISTORICAL UNIVERSE SURVIVORSHIP TEST")
    print("=" * 118)
    print(
        f"Period={START}..{END} | PIT proxy>={PROXY_THRESHOLD} | coverage>={MIN_COVERAGE}/8 | "
        f"TP=0.75ATR | SL=5% | TS=7 | risk=1.5% | max5 | costs={COST_BPS}bps RT"
    )
    print("NO strategy parameter is optimised in this run.\n")

    snapshot_dates, snapshot_sets, historical_universe = load_membership_snapshots()
    current_universe = {clean_symbol(t) for t in bt.load_tickers()}
    historical_sectors = load_historical_sector_map(historical_universe)
    current_sectors = pit_base.load_sector_map()
    current_sectors = {clean_symbol(k): v for k, v in current_sectors.items()}

    print(f"Historical canonical union: {len(historical_universe)}")
    print(f"Current universe:            {len(current_universe)}")
    print(f"Still current from history:  {len(historical_universe & current_universe)}")
    print(f"Removed historical names:   {len(historical_universe - current_universe)}")
    print(f"Historical sector metadata: {len(historical_sectors)}/{len(historical_universe)}")

    print("\nDownloading historical prices for the canonical historical union...")
    prices = bt.download_prices(sorted(historical_universe), start_date=PRICE_START, end_date=END)
    price_tickers = set(prices)
    removed = historical_universe - current_universe
    removed_with_prices = removed & price_tickers
    removed_missing_prices = removed - price_tickers
    print(f"Price series available: {len(price_tickers)}/{len(historical_universe)}")
    print(f"Removed names with prices: {len(removed_with_prices)}/{len(removed)}")
    print(f"Removed names still missing prices: {len(removed_missing_prices)}")

    membership_cov = annual_membership_coverage(snapshot_dates, snapshot_sets, price_tickers)
    print("\nPOINT-IN-TIME MEMBERSHIP PRICE COVERAGE")
    print(membership_cov.to_string(index=False))
    membership_cov.to_csv("sidi_full_available_membership_price_coverage.csv", index=False)

    print("\nBuilding indicators...")
    indicators = bt.build_indicators(prices)
    current_indicators = {
        t: ind for t, ind in indicators.items()
        if t in current_universe
    }

    # Technical signal construction: deliberately remove all fundamental filtering.
    tech_spec = exp.signal_variant(12.0, False)
    tech_spec["min_fund_score"] = None
    tech_full_raw, tech_full_raw_n = bt.build_signal_map(
        indicators,
        tech_spec,
        spy_dict={},
        date_from=START,
        date_to=END,
        fund_scores=None,
        sector_regime=None,
    )
    tech_current_raw, tech_current_raw_n = bt.build_signal_map(
        current_indicators,
        tech_spec,
        spy_dict={},
        date_from=START,
        date_to=END,
        fund_scores=None,
        sector_regime=None,
    )

    tech_full, full_member_stats = filter_exact_membership(
        tech_full_raw, snapshot_dates, snapshot_sets
    )
    date_added = partial_membership.load_date_added()
    tech_current, current_member_stats = partial_membership.filter_signal_map(
        tech_current_raw, date_added
    )
    print("\nTECHNICAL SIGNAL MEMBERSHIP FILTER")
    print(
        f"Current one-sided: raw={tech_current_raw_n}, kept={current_member_stats['kept']}, "
        f"pre-membership removed={current_member_stats['removed_pre_membership']}"
    )
    print(
        f"Full historical:   raw={tech_full_raw_n}, kept={full_member_stats['kept']}, "
        f"not-member removed={full_member_stats['removed_not_member_on_signal_date']}"
    )

    # Fetch statements for the union of current + historical names so the CURRENT
    # control is reproduced as faithfully as possible while FULL_AVAILABLE can score
    # removed constituents whenever yFinance still exposes their statements.
    statement_requested = historical_universe | current_universe
    print(f"\nFetching PIT statement histories for {len(statement_requested)} canonical tickers...")
    statement_cache, statement_failed = cal_ttm.fetch_full_statements(sorted(statement_requested))
    hist_stmt_audit = statement_audit(statement_cache, historical_universe)
    print("Historical statement coverage:")
    print(json.dumps(hist_stmt_audit, indent=2))

    state_cache = ttm_fast.build_state_cache(statement_cache, START, END)
    pmap = price_maps(prices)

    current_dates = sorted({d for sigs in tech_current.values() for d in sigs})
    full_dates = sorted({d for sigs in tech_full.values() for d in sigs})

    def active_current(asof: str):
        return {
            t for t in current_universe
            if (date_added.get(t) is None or asof >= date_added[t])
        }

    def active_full(asof: str):
        return membership_at(asof, snapshot_dates, snapshot_sets)

    print("\nScoring current-universe PIT control...")
    current_scores, current_cov = dynamic_scores(
        current_dates,
        active_current,
        current_sectors,
        state_cache,
        pmap,
        "CURRENT_ONE_SIDED",
    )
    print("\nScoring full-available historical PIT universe...")
    full_scores, full_cov = dynamic_scores(
        full_dates,
        active_full,
        historical_sectors,
        state_cache,
        pmap,
        "FULL_AVAILABLE",
    )
    coverage = pd.concat([current_cov, full_cov], ignore_index=True)
    coverage.to_csv("sidi_full_available_pit_coverage.csv", index=False)

    pit_current, pit_current_stats = filter_pit(tech_current, current_scores)
    pit_full, pit_full_stats = filter_pit(tech_full, full_scores)
    print("\nPIT FILTER")
    print("Current one-sided:")
    print(json.dumps(pit_current_stats, indent=2))
    print("Full available:")
    print(json.dumps(pit_full_stats, indent=2))

    # Static scores are irrelevant to the frozen non-ranking candidate. They are
    # passed only because the simulator API records a score field; PIT values are
    # patched into trade records after simulation.
    static_fund_scores = bt.load_fundamental_scores()

    run_specs = [
        ("CURRENT_TECH_ONLY", tech_current),
        ("FULL_AVAILABLE_TECH_ONLY", tech_full),
        ("CURRENT_PIT66", pit_current),
        ("FULL_AVAILABLE_PIT66", pit_full),
    ]
    summaries = []
    trade_files = {}
    print("\nFULL-PERIOD RESULTS")
    print("-" * 118)
    for name, smap in run_specs:
        st, trades = run(name, smap, prices, indicators, static_fund_scores, START, END)
        summaries.append(st)
        trade_files[name] = trades
        pd.DataFrame(trades).to_csv(f"sidi_{name.lower()}_trades.csv", index=False)
        print(
            f"{name:<27} trades={st['trades']:4d} WR={st['win_rate']:6.2f}% "
            f"PF={st['profit_factor']:5.3f} Ret={st['total_return']:8.2f}% "
            f"CAGR={st['cagr']:6.2f}% MDD={st['max_drawdown_mtm']:7.2f}% "
            f"Days={st['avg_days']:4.2f}"
        )

    by_name = {x["run_name"]: x for x in summaries}
    deltas = {
        "tech_only_full_minus_current": delta_row(
            by_name["CURRENT_TECH_ONLY"], by_name["FULL_AVAILABLE_TECH_ONLY"]
        ),
        "pit66_full_minus_current": delta_row(
            by_name["CURRENT_PIT66"], by_name["FULL_AVAILABLE_PIT66"]
        ),
    }
    print("\nSURVIVORSHIP SENSITIVITY — FULL AVAILABLE MINUS CURRENT CONTROL")
    print(json.dumps(deltas, indent=2))

    years = [
        ("2020", "2020-01-01", "2020-12-31"),
        ("2021", "2021-01-01", "2021-12-31"),
        ("2022", "2022-01-01", "2022-12-31"),
        ("2023", "2023-01-01", "2023-12-31"),
        ("2024_YTD", "2024-01-01", END),
    ]
    year_rows = []
    print("\nYEAR-BY-YEAR PIT66 — capital reset each year")
    print("-" * 118)
    for year, start, end in years:
        for name, smap in [
            ("CURRENT_PIT66", pit_current),
            ("FULL_AVAILABLE_PIT66", pit_full),
        ]:
            st, _ = run(name, smap, prices, indicators, static_fund_scores, start, end)
            st["year"] = year
            year_rows.append(st)
            print(
                f"{year:<8} {name:<24} trades={st['trades']:3d} WR={st['win_rate']:6.2f}% "
                f"PF={st['profit_factor']:5.3f} Ret={st['total_return']:7.2f}% "
                f"MDD={st['max_drawdown_mtm']:7.2f}% Days={st['avg_days']:4.2f}"
            )

    pd.DataFrame(summaries).drop(columns=["config"], errors="ignore").to_csv(
        "sidi_full_available_survivorship_summary.csv", index=False
    )
    pd.DataFrame(year_rows).drop(columns=["config"], errors="ignore").to_csv(
        "sidi_full_available_survivorship_by_year.csv", index=False
    )

    yearly_coverage = (
        coverage.assign(year=coverage["date"].str[:4])
        .groupby(["model", "year"])
        .agg(
            signal_dates=("date", "size"),
            active_with_statements=("active_with_statements", "mean"),
            mean_coverage=("mean_coverage", "mean"),
            coverage_ge6_pct=("coverage_ge6_pct", "mean"),
            score_ge_threshold=("score_ge_threshold", "mean"),
        )
        .reset_index()
    ) if not coverage.empty else pd.DataFrame()
    yearly_coverage.to_csv("sidi_full_available_pit_coverage_by_year.csv", index=False)

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "period": [START, END],
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
        "membership_source": {"repo": "hanshof/sp500_constituents", "commit": HANS_COMMIT},
        "metadata_source": {"repo": "lawcal/sp500-components-history", "commit": LAW_COMMIT},
        "universe": {
            "historical_canonical_union": len(historical_universe),
            "current_universe": len(current_universe),
            "historical_still_current": len(historical_universe & current_universe),
            "historical_removed": len(removed),
            "price_series_available": len(price_tickers),
            "removed_with_prices": len(removed_with_prices),
            "removed_missing_prices": len(removed_missing_prices),
            "removed_missing_price_tickers": sorted(removed_missing_prices),
        },
        "membership_price_coverage": membership_cov.to_dict("records"),
        "statement_history_audit": hist_stmt_audit,
        "statement_failures_count": len(statement_failed),
        "statement_failures": statement_failed,
        "technical_membership_filter": {
            "current_one_sided": current_member_stats,
            "full_historical": full_member_stats,
        },
        "fundamental_filter": {
            "current_one_sided": pit_current_stats,
            "full_available": pit_full_stats,
        },
        "summaries": summaries,
        "survivorship_deltas": deltas,
        "by_year": year_rows,
        "coverage_by_year": yearly_coverage.to_dict("records") if not yearly_coverage.empty else [],
        "limitations": [
            "This is FULL-AVAILABLE, not fully survivorship-free: removed constituents without Yahoo prices cannot trade.",
            "Historical yFinance statements can contain later restatements.",
            "45d quarterly / 90d annual lags approximate filing availability rather than use exact filing timestamps.",
            "Sector metadata is historical-constituent metadata but not a daily point-in-time GICS taxonomy reconstruction.",
            "Daily OHLC bars resolve same-day TP+SL ambiguity conservatively as STOP.",
        ],
    }
    Path("sidi_full_available_survivorship.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print("\nSaved full-available survivorship outputs.")


if __name__ == "__main__":
    main()
