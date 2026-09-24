"""Discover contextual features that may add edge to the frozen SIDI candidate.

DISCOVERY ONLY — no production filter is changed here.
All features are computed as-of the signal close, before T+1 entry.

Base candidate:
- DD60 >= 12%, RSI < 40 and existing technical setup
- current fund_score >= 6.5 (discovery sample only; PIT validation comes later)
- one-sided historical S&P membership correction
- entry T+1 open, TP 0.75xATR, SL 5%, time-stop 7, 1.5% risk
- 10 bps round-trip implementation cost

Features tested:
- relative DD60 vs sector ETF and SPY
- beta-adjusted 20d abnormal return (SPY + sector)
- DD60 z-score vs own trailing 252-session DD distribution
- prior 60d and 120d momentum
- abnormal volume (20d z-score and 5d/20d ratio)
- SPY regime, VIX percentile, market breadth and sector breadth
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import yfinance as yf

import backtest as bt
import backtest_experiments as exp
import validate_sidi_candidate as val
import historical_membership as membership

DISCOVERY_START = "2024-08-11"
DISCOVERY_END = "2026-09-10"
DOWNLOAD_START = "2023-01-01"
COST_BPS = 10

SECTOR_ETF = {
    "Information Technology": "XLK",
    "Technology": "XLK",
    "Financials": "XLF",
    "Energy": "XLE",
    "Health Care": "XLV",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
    "Materials": "XLB",
}

CANDIDATE = exp.Experiment(
    "CONTEXT_FEATURE_DISCOVERY",
    target_mode="atr", atr_mult=0.75,
    stop_mode="fixed", stop_pct=0.05,
    time_stop=7,
    description="Frozen SIDI exit for contextual feature discovery",
)

FEATURES = [
    "rel_dd_sector_pp", "rel_dd_spy_pp", "abnormal_return_20d_pct",
    "dd60_zscore", "momentum_60d_pct", "momentum_120d_pct",
    "volume_z20", "volume_ratio_5_20", "spy_20d_pct",
    "vix_percentile_252", "market_breadth_sma50", "sector_breadth_sma50",
]


def load_master():
    files = sorted(Path("data/master").glob("sp500_full_export_*.csv"))
    if not files:
        raise FileNotFoundError("No master CSV")
    df = pd.read_csv(files[-1])
    df["ticker"] = df["ticker"].astype(str).str.upper()
    sectors = dict(zip(df.ticker, df.get("sector", pd.Series("Unknown", index=df.index)).fillna("Unknown")))
    return df, sectors


def to_indexed(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    x["Date"] = pd.to_datetime(x["Date"]).dt.tz_localize(None)
    x = x.set_index("Date").sort_index()
    return x


def download_reference(symbol: str) -> pd.DataFrame:
    try:
        d = yf.download(symbol, start=DOWNLOAD_START, end="2026-09-11", auto_adjust=False,
                        progress=False, threads=False)
        if d is None or d.empty:
            return pd.DataFrame()
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        d = d.reset_index()
        return to_indexed(d)
    except Exception as exc:
        print(f"WARN reference {symbol}: {type(exc).__name__}: {exc}")
        return pd.DataFrame()


def row_on_or_before(df: pd.DataFrame, date: str):
    if df is None or df.empty:
        return None
    ts = pd.Timestamp(date)
    sub = df.loc[df.index <= ts]
    return sub.iloc[-1] if len(sub) else None


def pos_on_or_before(df: pd.DataFrame, date: str):
    if df is None or df.empty:
        return None
    ts = pd.Timestamp(date)
    p = df.index.searchsorted(ts, side="right") - 1
    return int(p) if p >= 0 else None


def ret_n(df: pd.DataFrame, date: str, n: int) -> float:
    p = pos_on_or_before(df, date)
    if p is None or p < n:
        return np.nan
    c0, c1 = float(df.iloc[p-n]["Close"]), float(df.iloc[p]["Close"])
    return (c1 / c0 - 1.0) if c0 else np.nan


def dd_n(df: pd.DataFrame, date: str, n: int = 60) -> float:
    p = pos_on_or_before(df, date)
    if p is None or p < n-1:
        return np.nan
    closes = pd.to_numeric(df.iloc[p-n+1:p+1]["Close"], errors="coerce")
    if closes.isna().all():
        return np.nan
    mx = float(closes.max()); cur = float(closes.iloc[-1])
    return (cur / mx - 1.0) * 100.0 if mx else np.nan


def dd60_zscore(df: pd.DataFrame, date: str) -> float:
    p = pos_on_or_before(df, date)
    if p is None or p < 310:
        return np.nan
    closes = pd.to_numeric(df.iloc[:p+1]["Close"], errors="coerce")
    dd = (closes / closes.rolling(60).max() - 1.0) * 100.0
    hist = dd.iloc[max(0, p-251):p]  # strictly before current signal observation
    hist = hist.dropna()
    if len(hist) < 100 or hist.std(ddof=0) == 0:
        return np.nan
    return float((dd.iloc[p] - hist.mean()) / hist.std(ddof=0))


def volume_features(df: pd.DataFrame, date: str):
    p = pos_on_or_before(df, date)
    if p is None or p < 20:
        return np.nan, np.nan
    v = pd.to_numeric(df.iloc[:p+1]["Volume"], errors="coerce")
    prev = v.iloc[p-20:p]
    sd = prev.std(ddof=0)
    z = (v.iloc[p] - prev.mean()) / sd if pd.notna(sd) and sd > 0 else np.nan
    m5 = v.iloc[p-4:p+1].mean() if p >= 4 else np.nan
    m20 = v.iloc[p-19:p+1].mean()
    ratio = m5 / m20 if pd.notna(m20) and m20 != 0 else np.nan
    return float(z) if pd.notna(z) else np.nan, float(ratio) if pd.notna(ratio) else np.nan


def beta_adjusted_abnormal(stock: pd.DataFrame, spy: pd.DataFrame, sector: pd.DataFrame, date: str) -> float:
    """20-session abnormal return using betas estimated on the preceding 120 sessions."""
    if stock.empty or spy.empty or sector.empty:
        return np.nan
    s = stock[["Close"]].rename(columns={"Close":"s"})
    m = spy[["Close"]].rename(columns={"Close":"m"})
    q = sector[["Close"]].rename(columns={"Close":"q"})
    z = s.join(m, how="inner").join(q, how="inner").loc[:pd.Timestamp(date)].dropna()
    if len(z) < 150:
        return np.nan
    r = z.pct_change().dropna()
    if len(r) < 140:
        return np.nan
    # Exclude the most recent 20 sessions from beta estimation.
    train = r.iloc[-140:-20]
    recent = r.iloc[-20:]
    X = np.column_stack([np.ones(len(train)), train["m"].to_numpy(), train["q"].to_numpy()])
    try:
        b = np.linalg.lstsq(X, train["s"].to_numpy(), rcond=None)[0]
    except Exception:
        return np.nan
    expected = b[0] * len(recent) + b[1] * recent["m"].sum() + b[2] * recent["q"].sum()
    actual = recent["s"].sum()
    return float((actual - expected) * 100.0)


def percentile_asof(series: pd.Series, date: str, lookback: int = 252) -> float:
    s = series.loc[:pd.Timestamp(date)].dropna()
    if len(s) < 80:
        return np.nan
    s = s.iloc[-lookback:]
    cur = float(s.iloc[-1])
    return float((s <= cur).mean() * 100.0)


def breadth_maps(prices_idx: dict[str, pd.DataFrame], sectors: dict, added: dict):
    records = []
    for ticker, df in prices_idx.items():
        if df.empty:
            continue
        c = pd.to_numeric(df["Close"], errors="coerce")
        sma50 = c.rolling(50).mean()
        joined = added.get(ticker)
        for dt, close, ma in zip(df.index, c, sma50):
            if pd.isna(close) or pd.isna(ma):
                continue
            ds = dt.date().isoformat()
            if joined is not None and ds < joined:
                continue
            records.append((dt, ticker, sectors.get(ticker, "Unknown"), float(close > ma)))
    b = pd.DataFrame(records, columns=["date","ticker","sector","above50"])
    if b.empty:
        return {}, {}
    market = b.groupby("date")["above50"].mean().mul(100).to_dict()
    sector = b.groupby(["date","sector"])["above50"].mean().mul(100).to_dict()
    return market, sector


def pf_from_pnl(x: pd.Series) -> float:
    x = pd.to_numeric(x, errors="coerce").dropna()
    gp = x[x > 0].sum(); gl = -x[x < 0].sum()
    return float(gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)


def bucket_table(df: pd.DataFrame):
    rows = []
    for feature in FEATURES:
        z = df[[feature,"pnl_eur","outcome"]].dropna().copy()
        if len(z) < 80 or z[feature].nunique() < 4:
            continue
        try:
            z["bucket"] = pd.qcut(z[feature], 4, duplicates="drop")
        except Exception:
            continue
        groups = list(z.groupby("bucket", observed=True))
        for rank, (bucket, g) in enumerate(groups, 1):
            rows.append({
                "feature": feature, "quartile": rank, "range": str(bucket),
                "n": len(g), "mean_feature": float(g[feature].mean()),
                "win_rate": float((g.outcome == "WIN").mean()*100),
                "profit_factor": pf_from_pnl(g.pnl_eur),
                "expectancy_eur": float(g.pnl_eur.mean()),
                "total_pnl_eur": float(g.pnl_eur.sum()),
            })
    return pd.DataFrame(rows)


def feature_summary(df: pd.DataFrame, buckets: pd.DataFrame):
    rows = []
    y = (df.outcome == "WIN").astype(float)
    for feature in FEATURES:
        x = pd.to_numeric(df[feature], errors="coerce")
        ok = x.notna() & y.notna()
        if ok.sum() < 80:
            continue
        rank_corr = x[ok].rank().corr(y[ok].rank())
        b = buckets[buckets.feature == feature].sort_values("quartile")
        wr_spread = float(b.win_rate.max()-b.win_rate.min()) if len(b) else np.nan
        pf_spread = float(b.profit_factor.replace([np.inf,-np.inf],np.nan).max()-b.profit_factor.replace([np.inf,-np.inf],np.nan).min()) if len(b) else np.nan
        exp_spread = float(b.expectancy_eur.max()-b.expectancy_eur.min()) if len(b) else np.nan
        rows.append({
            "feature": feature, "n": int(ok.sum()), "spearman_vs_win": float(rank_corr),
            "wr_quartile_spread_pp": wr_spread, "pf_quartile_spread": pf_spread,
            "expectancy_quartile_spread_eur": exp_spread,
        })
    return pd.DataFrame(rows).sort_values(["wr_quartile_spread_pp","expectancy_quartile_spread_eur"], ascending=False)


def main():
    print("="*112)
    print("SIDI CONTEXT FEATURE DISCOVERY — RELATIVE DD / ABNORMAL RETURN / REGIME")
    print("="*112)
    print("Discovery only: no feature is promoted to a production filter in this run.\n")

    master, sectors = load_master()
    tickers = bt.load_tickers()
    fund_scores = bt.load_fundamental_scores()
    added = membership.load_date_added()

    prices = bt.download_prices(tickers, start_date=DOWNLOAD_START, end_date=DISCOVERY_END)
    indicators = bt.build_indicators(prices)
    prices_idx = {t: to_indexed(d) for t,d in prices.items()}

    spec = exp.signal_variant(12.0, False)
    signal_raw, raw_n = bt.build_signal_map(
        indicators, spec, spy_dict={}, date_from=DISCOVERY_START, date_to=DISCOVERY_END,
        fund_scores=fund_scores, sector_regime=None,
    )
    signal_map, member_stats = membership.filter_signal_map(signal_raw, added)
    print(f"Signals raw={raw_n}, membership-kept={member_stats['kept']}, removed={member_stats['removed_pre_membership']}")

    stats, trades = val.run_sim(signal_map, prices, indicators, fund_scores, CANDIDATE,
                                DISCOVERY_START, DISCOVERY_END, cost_bps=COST_BPS)
    tdf = pd.DataFrame(trades)
    print(f"Executed trades={len(tdf)} WR={stats['win_rate']:.2f}% PF={stats['profit_factor']:.3f} Ret={stats['total_return']:.2f}% MDD={stats['max_drawdown_mtm']:.2f}%")

    refs = {s: download_reference(s) for s in sorted(set(SECTOR_ETF.values()) | {"SPY","^VIX"})}
    spy = refs["SPY"]; vix = refs["^VIX"]
    vix_series = pd.to_numeric(vix["Close"], errors="coerce") if not vix.empty else pd.Series(dtype=float)
    spy_close = pd.to_numeric(spy["Close"], errors="coerce") if not spy.empty else pd.Series(dtype=float)
    spy_sma200 = spy_close.rolling(200).mean() if len(spy_close) else pd.Series(dtype=float)

    print("Computing market/sector breadth...")
    market_breadth, sector_breadth = breadth_maps(prices_idx, sectors, added)

    feats = []
    for i, tr in tdf.iterrows():
        ticker = str(tr.ticker); d = str(tr.signal_date)[:10]
        sdf = prices_idx.get(ticker, pd.DataFrame())
        sector = sectors.get(ticker, "Unknown")
        etf_sym = SECTOR_ETF.get(sector)
        edf = refs.get(etf_sym, pd.DataFrame()) if etf_sym else pd.DataFrame()

        stock_dd = dd_n(sdf, d, 60)
        sec_dd = dd_n(edf, d, 60)
        spy_dd = dd_n(spy, d, 60)
        vz, vr = volume_features(sdf, d)
        ts = pd.Timestamp(d)
        spy20 = ret_n(spy, d, 20)
        spy_bull = np.nan
        if ts in spy_close.index and ts in spy_sma200.index and pd.notna(spy_sma200.loc[ts]):
            spy_bull = float(spy_close.loc[ts] > spy_sma200.loc[ts])
        elif len(spy_close.loc[:ts]) and len(spy_sma200.loc[:ts].dropna()):
            spy_bull = float(spy_close.loc[:ts].iloc[-1] > spy_sma200.loc[:ts].dropna().iloc[-1])

        feats.append({
            "ticker": ticker, "signal_date": d, "sector": sector, "sector_etf": etf_sym,
            "stock_dd60_pct": stock_dd, "sector_dd60_pct": sec_dd, "spy_dd60_pct": spy_dd,
            "rel_dd_sector_pp": stock_dd-sec_dd if pd.notna(stock_dd) and pd.notna(sec_dd) else np.nan,
            "rel_dd_spy_pp": stock_dd-spy_dd if pd.notna(stock_dd) and pd.notna(spy_dd) else np.nan,
            "abnormal_return_20d_pct": beta_adjusted_abnormal(sdf, spy, edf, d),
            "dd60_zscore": dd60_zscore(sdf, d),
            "momentum_60d_pct": ret_n(sdf, d, 60)*100 if pd.notna(ret_n(sdf,d,60)) else np.nan,
            "momentum_120d_pct": ret_n(sdf, d, 120)*100 if pd.notna(ret_n(sdf,d,120)) else np.nan,
            "volume_z20": vz, "volume_ratio_5_20": vr,
            "spy_20d_pct": spy20*100 if pd.notna(spy20) else np.nan,
            "spy_above_sma200": spy_bull,
            "vix_percentile_252": percentile_asof(vix_series, d, 252) if len(vix_series) else np.nan,
            "market_breadth_sma50": market_breadth.get(ts, np.nan),
            "sector_breadth_sma50": sector_breadth.get((ts, sector), np.nan),
        })
        if (i+1) % 100 == 0:
            print(f"  features {i+1}/{len(tdf)}")

    fdf = pd.DataFrame(feats)
    out = tdf.merge(fdf, on=["ticker","signal_date"], how="left")
    buckets = bucket_table(out)
    summary = feature_summary(out, buckets)

    # Binary SPY regime diagnostic separately.
    regime_rows = []
    for valx, g in out.dropna(subset=["spy_above_sma200"]).groupby("spy_above_sma200"):
        regime_rows.append({
            "feature":"spy_above_sma200", "group":"above" if valx==1 else "below",
            "n":len(g), "win_rate":float((g.outcome=="WIN").mean()*100),
            "profit_factor":pf_from_pnl(g.pnl_eur), "expectancy_eur":float(g.pnl_eur.mean()),
        })
    regime = pd.DataFrame(regime_rows)

    out.to_csv("sidi_context_feature_trades.csv", index=False)
    buckets.to_csv("sidi_context_feature_buckets.csv", index=False)
    summary.to_csv("sidi_context_feature_summary.csv", index=False)
    regime.to_csv("sidi_context_feature_regimes.csv", index=False)
    Path("sidi_context_feature_analysis.json").write_text(json.dumps({
        "generated_at": datetime.utcnow().isoformat()+"Z",
        "period":[DISCOVERY_START,DISCOVERY_END],
        "candidate_stats":stats,
        "membership":member_stats,
        "features":FEATURES,
        "method_note":"Discovery sample only. Promote no threshold without OOS confirmation.",
        "top_features":summary.head(12).to_dict(orient="records"),
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print("\nFEATURE RANKING (discovery only)")
    print(summary.to_string(index=False))
    print("\nQUARTILE TABLE")
    print(buckets.to_string(index=False))
    print("\nSPY REGIME")
    print(regime.to_string(index=False))
    print("\nSaved feature trade dataset, buckets, summary, regimes and JSON.")


if __name__ == "__main__":
    main()
