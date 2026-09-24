"""Full SIDI validation using a DYNAMIC yFinance PIT fundamental proxy.

This is the first backtest in this repo where historical technical signals are
NOT filtered using today's fund_score. Instead, for every historical signal
date we reconstruct a conservative annual-fundamental snapshot using only
fiscal periods that ended >=90 days earlier, calculate sector medians for that
historical date, and apply SIDI's existing scoring formula.

It remains a PIT_PROXY rather than PIT_TRUE because historical statement values
served today may include later restatements and the universe is today's S&P 500.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

import backtest as bt
import backtest_experiments as exp
import validate_sidi_candidate as val
import yfinance_pit_proxy as pit
from modules.ingesta.scoring import _sector_medians, _fund_score

START = "2024-08-11"
END = "2026-09-10"
LAG_DAYS = 90
COST_BPS = 10
MIN_SCORE = 6.5
MIN_COVERAGE = 6

CANDIDATE = exp.Experiment(
    "YF_PIT_PROXY_ATR075_T7",
    target_mode="atr", atr_mult=0.75,
    stop_mode="fixed", stop_pct=0.05,
    time_stop=7,
    description="Dynamic annual PIT proxy fund>=6.5 + TP0.75ATR + SL5 + TS7",
)


def fetch_statement_cache(tickers: list[str]) -> tuple[dict, list[str]]:
    cache = {}
    failed = []
    n = len(tickers)
    for i, ticker in enumerate(tickers, 1):
        try:
            t = yf.Ticker(ticker.replace("-", "."))
            inc = t.income_stmt
            bal = t.balance_sheet
            cf = t.cashflow
            if (inc is None or inc.empty) and (bal is None or bal.empty):
                raise ValueError("empty statements")
            cache[ticker] = {"inc": inc, "bal": bal, "cf": cf}
        except Exception as exc:
            failed.append(ticker)
            if len(failed) <= 20:
                print(f"  WARN statements {ticker}: {type(exc).__name__}: {exc}")
        if i % 25 == 0 or i == n:
            print(f"  Statements {i}/{n}: OK={len(cache)} fail={len(failed)}")
        time.sleep(0.06)
    return cache, failed


def price_maps(prices: dict[str, pd.DataFrame]):
    out = {}
    for ticker, df in prices.items():
        rows = {}
        for _, r in df.iterrows():
            d = str(r["Date"])[:10]
            rows[d] = float(r["Close"])
        out[ticker] = rows
    return out


def close_on_or_before(rows: dict[str, float], asof: str) -> float:
    if asof in rows:
        return rows[asof]
    candidates = [d for d in rows.keys() if d <= asof]
    return rows[max(candidates)] if candidates else np.nan


def metrics_from_cache(ticker: str, sector: str, asof: str, statements: dict, prices_by_date: dict) -> dict:
    s = statements.get(ticker)
    if not s:
        return {"ticker": ticker, "sector": sector, "metric_coverage": 0}
    inc, bal, cf = s["inc"], s["bal"], s["cf"]

    revenue, revenue_prev, income_period = pit._latest_two(
        inc, ["Total Revenue", "Operating Revenue", "Revenue"], asof
    )
    ni, ni_prev, _ = pit._latest_two(
        inc, ["Net Income", "Net Income Common Stockholders", "Net Income Including Noncontrolling Interests"], asof
    )
    eps, eps_prev, _ = pit._latest_two(inc, ["Diluted EPS", "Basic EPS"], asof)

    equity, balance_period = pit._latest(
        bal, ["Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity"], asof
    )
    ca, _ = pit._latest(bal, ["Current Assets", "Total Current Assets"], asof)
    cl, _ = pit._latest(bal, ["Current Liabilities", "Total Current Liabilities"], asof)
    total_debt, _ = pit._latest(
        bal, ["Total Debt", "Total Debt And Capital Lease Obligation", "Long Term Debt And Capital Lease Obligation"], asof
    )
    shares, _ = pit._latest(
        bal, ["Ordinary Shares Number", "Share Issued", "Common Stock Shares Outstanding"], asof
    )

    fcf, cf_period = pit._latest(cf, ["Free Cash Flow"], asof)
    if pd.isna(fcf):
        ocf, cf_period = pit._latest(cf, ["Operating Cash Flow", "Total Cash From Operating Activities"], asof)
        capex, _ = pit._latest(cf, ["Capital Expenditure", "Capital Expenditures"], asof)
        if pd.notna(ocf) and pd.notna(capex):
            fcf = float(ocf + capex if capex < 0 else ocf - capex)

    price = close_on_or_before(prices_by_date.get(ticker, {}), asof)
    market_cap = price * shares if pd.notna(price) and pd.notna(shares) else np.nan

    revenue_growth = (
        (revenue - revenue_prev) / abs(revenue_prev)
        if pd.notna(revenue) and pd.notna(revenue_prev) and revenue_prev != 0 else np.nan
    )
    eps_growth = (
        (eps - eps_prev) / abs(eps_prev)
        if pd.notna(eps) and pd.notna(eps_prev) and eps_prev != 0 else np.nan
    )
    eps_growth_source = "eps"
    if pd.isna(eps_growth) and pd.notna(ni) and pd.notna(ni_prev) and ni_prev != 0:
        eps_growth = (ni - ni_prev) / abs(ni_prev)
        eps_growth_source = "net_income_proxy"

    roe = pit._safe_div(ni, equity)
    debt_equity = pit._safe_div(total_debt, equity, 100.0)
    current_ratio = pit._safe_div(ca, cl)
    fcf_ni_ratio = pit._safe_div(fcf, ni)
    pe = pit._safe_div(price, eps)
    pb = pit._safe_div(market_cap, equity)

    metrics = [revenue_growth, eps_growth, roe, debt_equity, current_ratio, fcf_ni_ratio, pe, pb]
    coverage = sum(pd.notna(v) for v in metrics)

    return {
        "ticker": ticker,
        "sector": sector,
        "asof_date": asof,
        "metric_coverage": coverage,
        "income_period_used": income_period.date().isoformat() if income_period is not None else None,
        "balance_period_used": balance_period.date().isoformat() if balance_period is not None else None,
        "cashflow_period_used": cf_period.date().isoformat() if cf_period is not None else None,
        "eps_growth_source": eps_growth_source,
        "revenue_growth": revenue_growth,
        "eps_growth": eps_growth,
        "roe": roe,
        "debt_equity": debt_equity,
        "current_ratio": current_ratio,
        "fcf_ni_ratio": fcf_ni_ratio,
        "pe": pe,
        "pb": pb,
        "fcf": fcf,
        "market_cap": market_cap,
    }


def dynamic_scores(signal_dates: list[str], tickers: list[str], sectors: dict,
                   statement_cache: dict, prices_by_date: dict):
    score_by_date = {}
    coverage_rows = []
    n = len(signal_dates)
    for i, asof in enumerate(signal_dates, 1):
        rows = [
            metrics_from_cache(t, sectors.get(t, "Unknown"), asof, statement_cache, prices_by_date)
            for t in tickers
        ]
        df = pd.DataFrame(rows)
        sm = _sector_medians(df)
        scores = df.apply(lambda r: _fund_score(r, sm), axis=1)
        df = pd.concat([df, scores], axis=1)
        score_by_date[asof] = {
            str(r.ticker): {
                "fund_score": float(r.fund_score),
                "coverage": int(r.metric_coverage),
            }
            for r in df.itertuples()
        }
        coverage_rows.append({
            "date": asof,
            "mean_coverage": float(df["metric_coverage"].mean()),
            "coverage_ge6_pct": float((df["metric_coverage"] >= MIN_COVERAGE).mean() * 100),
            "score_ge65": int((df["fund_score"] >= MIN_SCORE).sum()),
            "score_median": float(df["fund_score"].median()),
        })
        if i % 25 == 0 or i == n:
            c = coverage_rows[-1]
            print(
                f"  PIT scoring {i}/{n} {asof}: coverage={c['mean_coverage']:.2f}/8 "
                f">=6={c['coverage_ge6_pct']:.1f}% score>=6.5={c['score_ge65']}"
            )
    return score_by_date, pd.DataFrame(coverage_rows)


def filter_signal_map(base_signal_map: dict, score_by_date: dict, require_coverage: bool):
    out = {}
    kept = 0
    for ticker, sigs in base_signal_map.items():
        for date, sig in sigs.items():
            rec = score_by_date.get(date, {}).get(ticker)
            if not rec:
                continue
            if rec["fund_score"] < MIN_SCORE:
                continue
            if require_coverage and rec["coverage"] < MIN_COVERAGE:
                continue
            item = dict(sig)
            item["pit_fund_score"] = rec["fund_score"]
            item["pit_coverage"] = rec["coverage"]
            out.setdefault(ticker, {})[date] = item
            kept += 1
    return out, kept


def current_proxy_signal_map(indicators, fund_scores):
    spec = exp.signal_variant(12.0, False)
    spec["min_fund_score"] = 6.5
    return bt.build_signal_map(
        indicators, spec, spy_dict={}, date_from=START, date_to=END,
        fund_scores=fund_scores, sector_regime=None,
    )


def run_variant(name: str, signal_map: dict, prices, indicators, fund_scores):
    e = exp.Experiment(
        name,
        target_mode="atr", atr_mult=0.75,
        stop_mode="fixed", stop_pct=0.05,
        time_stop=7,
        description=name,
    )
    st, trades = val.run_sim(
        signal_map, prices, indicators, fund_scores, e,
        START, END, cost_bps=COST_BPS,
    )
    return st, trades


def main():
    print("=" * 105)
    print("SIDI DYNAMIC YFINANCE PIT-PROXY BACKTEST")
    print("=" * 105)
    print(f"Period {START}..{END} | lag={LAG_DAYS}d | cost={COST_BPS}bps RT | score>={MIN_SCORE}")
    print("WARNING: PIT_PROXY uses current yFinance historical statements and current universe; not PIT_TRUE.\n")

    tickers = bt.load_tickers()
    fund_scores = bt.load_fundamental_scores()
    prices = bt.download_prices(tickers, start_date=START, end_date=END)
    indicators = bt.build_indicators(prices)
    prices_by_date = price_maps(prices)

    sectors = pit.load_sector_map()

    tech_spec = exp.signal_variant(12.0, False)
    tech_spec["min_fund_score"] = 0.0
    tech_signal_map, tech_n = bt.build_signal_map(
        indicators, tech_spec, spy_dict={}, date_from=START, date_to=END,
        fund_scores=fund_scores, sector_regime=None,
    )
    signal_dates = sorted({d for sigs in tech_signal_map.values() for d in sigs})
    signal_tickers = sorted(tech_signal_map.keys())
    print(f"Technical DD12 signals: {tech_n}; unique signal dates={len(signal_dates)}; signal tickers={len(signal_tickers)}")

    # Fetch only stocks that ever generated a technical signal; non-signal stocks still
    # matter for sector medians, so fetch the full universe where possible.
    statement_cache, failed_statements = fetch_statement_cache(tickers)
    print(f"Statement cache: {len(statement_cache)}/{len(tickers)}; failed={len(failed_statements)}")

    score_by_date, coverage_df = dynamic_scores(
        signal_dates, tickers, sectors, statement_cache, prices_by_date
    )
    coverage_df.to_csv("sidi_yf_pit_proxy_coverage.csv", index=False)

    pit_map, pit_n = filter_signal_map(tech_signal_map, score_by_date, require_coverage=False)
    pit_cov_map, pit_cov_n = filter_signal_map(tech_signal_map, score_by_date, require_coverage=True)
    current_map, current_n = current_proxy_signal_map(indicators, fund_scores)

    variants = [
        ("TECH_NO_FUND", tech_signal_map, tech_n),
        ("CURRENT_PROXY_65", current_map, current_n),
        ("YF_PIT_PROXY_65", pit_map, pit_n),
        ("YF_PIT_PROXY_65_COV6", pit_cov_map, pit_cov_n),
    ]

    summaries = []
    details = {}
    print("\nRESULTS — SAME EXIT/RISK/COST, ONLY FUNDAMENTAL FILTER CHANGES")
    print("-" * 105)
    for name, smap, n in variants:
        st, trades = run_variant(name, smap, prices, indicators, fund_scores)
        st["builder_signals"] = n
        summaries.append(st)
        details[name] = trades
        print(
            f"{name:<24} sig={n:5d} trades={st['trades']:4d} WR={st['win_rate']:6.2f}% "
            f"PF={st['profit_factor']:5.2f} Ret={st['total_return']:8.2f}% "
            f"MDD={st['max_drawdown_mtm']:7.2f}% Days={st['avg_days']:4.2f}"
        )

    pd.DataFrame(summaries).drop(columns=["config"], errors="ignore").to_csv(
        "sidi_yf_pit_proxy_summary.csv", index=False
    )
    Path("sidi_yf_pit_proxy_results.json").write_text(json.dumps({
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "method": {
            "label": "PIT_PROXY",
            "annual_publication_lag_days": LAG_DAYS,
            "score_threshold": MIN_SCORE,
            "coverage_floor_variant": MIN_COVERAGE,
            "round_trip_cost_bps": COST_BPS,
            "remaining_biases": [
                "historical yFinance statements may include later restatements",
                "current S&P 500 universe creates survivorship bias",
                "90-day lag approximates, not observes, actual filing dates",
            ],
        },
        "failed_statement_tickers": failed_statements,
        "summaries": summaries,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nSaved summary, coverage and JSON results.")


if __name__ == "__main__":
    main()
