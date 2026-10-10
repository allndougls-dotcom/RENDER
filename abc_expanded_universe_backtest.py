#!/usr/bin/env python3
"""
SIDI_INTRADAY_V2 · ABC Expanded Universe Backtest

Pregunta:
¿Puede el filtro más restrictivo ABC conservar su calidad si ampliamos
el universo desde S&P 500 a S&P Composite 1500 (500+400+600)?

ABC congelado:
A = range_pos60 >= 0.088
B = gap_pct >= -0.17
C = 0.848 <= volume_ratio_5_60 < 1.0

Metodología:
- mismo contrato SIDI_INTRADAY_V2;
- mismo fund_score >= 6.5;
- las nuevas compañías se puntúan contra las medianas sectoriales del
  S&P 500 maestro actual para preservar la escala del umbral 6.5;
- fundamentales sólo para tickers que sobreviven técnico+contexto+ABC;
- cartera fija 1.5% riesgo/trade, 4.5% cap, 3 posiciones;
- 8 años de histórico;
- universo de componentes ACTUALES => survivorship bias conocido,
  igual que el backtest S&P500 existente.
"""
from __future__ import annotations

import io
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from backtest import build_indicators
from modules.ingesta.fundamental import _fund_yf
from modules.ingesta.scoring import _fund_score, _sector_medians
from modules.ingesta.sidi_context import SECTOR_ETF_MAP, _abnormal20, _close_series, _spy20
from pattern_backtest import build_episode_dataset, enrich_signals, metrics as episode_metrics
from risk_matrix_backtest import (
    download_context_benchmarks,
    make_price_maps,
    norm_ticker,
    simulate_configuration,
)
from stabilization_gate_backtest import (
    A_RANGE_POS60_MIN,
    B_GAP_PCT_MIN,
    C_VOL_RATIO_MIN,
    C_VOL_RATIO_MAX,
    to_entries_by_date,
)

STRATEGY_VERSION = "SIDI_INTRADAY_V2"
MODEL_VERSION = "SIDI_ABC_EXPANDED_UNIVERSE_V2"  # same model, faster IO only
YEARS = 8
TRADE_RISK_PCT = 1.5
PORTFOLIO_CAP_PCT = 4.5
OUT_DIR = Path("data/abc_expanded_universe")

INDEX_URLS = {
    "SP500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "SP400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "SP600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}


def fetch_index(index_name, url):
    headers = {"User-Agent": "Mozilla/5.0 SIDI-backtest/1.0"}
    r = requests.get(url, headers=headers, timeout=45)
    r.raise_for_status()
    tables = pd.read_html(io.StringIO(r.text))
    best = None
    for df in tables:
        cols = [str(c).strip() for c in df.columns]
        low = [c.lower() for c in cols]
        has_symbol = any("symbol" in c or "ticker" in c for c in low)
        has_sector = any("sector" in c for c in low)
        if has_symbol and has_sector and len(df) > 200:
            best = df.copy()
            best.columns = cols
            break
    if best is None:
        raise RuntimeError(f"No se pudo localizar tabla de {index_name}")

    cmap = {}
    for c in best.columns:
        cl = c.lower()
        if "symbol" in cl or "ticker" in cl:
            cmap[c] = "ticker"
        elif "security" in cl or "company" in cl:
            cmap[c] = "name"
        elif "gics sector" in cl or ("sector" in cl and "sub" not in cl):
            cmap[c] = "sector"
        elif "sub" in cl and ("industry" in cl or "sector" in cl):
            cmap[c] = "industry"
    best = best.rename(columns=cmap)
    if "ticker" not in best.columns or "sector" not in best.columns:
        raise RuntimeError(f"Columnas incompletas en {index_name}: {best.columns.tolist()}")
    keep = [c for c in ["ticker","name","sector","industry"] if c in best.columns]
    best = best[keep].copy()
    best["ticker"] = best["ticker"].astype(str).map(norm_ticker)
    best["sector"] = best["sector"].astype(str).str.strip()
    best["index_bucket"] = index_name
    return best.drop_duplicates("ticker")



def fast_download_prices(tickers, years=8):
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today()-timedelta(days=365*years+30)).strftime("%Y-%m-%d")
    tickers=[norm_ticker(t) for t in tickers]
    prices={}
    chunk_size=50
    for i in range(0,len(tickers),chunk_size):
        chunk=tickers[i:i+chunk_size]
        n=i//chunk_size+1
        total=(len(tickers)+chunk_size-1)//chunk_size
        print(f"  Fast price batch {n}/{total} ({len(chunk)})...")
        raw=None
        for attempt in range(3):
            try:
                raw=yf.download(
                    chunk,start=start,end=end,auto_adjust=True,progress=False,
                    group_by="ticker",threads=True
                )
                if raw is not None and not raw.empty:
                    break
            except Exception as e:
                print(f"    retry {attempt+1}: {e}")
            time.sleep(1.5*(attempt+1))
        if raw is None or raw.empty:
            continue
        for t in chunk:
            try:
                df=raw[t].copy() if isinstance(raw.columns,pd.MultiIndex) else raw.copy()
                df=df.reset_index()
                if "Date" not in df.columns:
                    continue
                df=df.dropna(subset=["Close"])
                if len(df)>170:
                    prices[t]=df.reset_index(drop=True)
            except Exception:
                continue
        time.sleep(0.5)
    print(f"  Fast prices OK: {len(prices)}/{len(tickers)}")
    return prices


def fast_candidate_fundamentals(candidate_tickers, universe, medians):
    tickers=sorted(candidate_tickers)
    print(f"  Fast fundamentals: {len(tickers)} candidate tickers...")
    rows=[]
    def one(t):
        d=_fund_yf(t)
        d["ticker"]=t
        return d
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs={ex.submit(one,t):t for t in tickers}
        for idx,fut in enumerate(as_completed(futs),1):
            t=futs[fut]
            try:
                rows.append(fut.result())
            except Exception:
                rows.append({"ticker":t})
            if idx%25==0:
                print(f"    fundamentals {idx}/{len(tickers)}")
    fund=pd.DataFrame(rows)
    sectors=universe[["ticker","sector"]].drop_duplicates("ticker")
    fund=fund.merge(sectors,on="ticker",how="left")
    scored=[]
    for _,row in fund.iterrows():
        score=_fund_score(row,medians)
        d=row.to_dict()
        d.update(score.to_dict())
        scored.append(d)
    return pd.DataFrame(scored)


def build_universe():
    frames = []
    for name, url in INDEX_URLS.items():
        df = fetch_index(name, url)
        print(f"  {name}: {len(df)}")
        frames.append(df)
    all_df = pd.concat(frames, ignore_index=True)
    # Si algún ticker aparece en más de un índice por transición, prioriza SP500>SP400>SP600.
    rank = {"SP500":0, "SP400":1, "SP600":2}
    all_df["_rank"] = all_df["index_bucket"].map(rank)
    all_df = all_df.sort_values("_rank").drop_duplicates("ticker").drop(columns="_rank")
    print(f"  Universo combinado único: {len(all_df)}")
    return all_df.reset_index(drop=True)


def build_prefund_signals(prices, indicators, universe, benchmarks):
    """Contrato FULL salvo fund_score; devuelve candidatos técnico+contexto."""
    meta = universe.set_index("ticker").to_dict("index")
    spy_close_all = benchmarks["SPY"]
    entries = {}
    counts = {
        "technical_candidates":0,
        "context_ready":0,
        "prefund_full":0,
        "missing_sector_context":0,
    }

    for ticker, ind in indicators.items():
        m = meta.get(ticker)
        if not m:
            continue
        sector = str(m.get("sector",""))
        sector_etf = SECTOR_ETF_MAP.get(sector)
        sector_close_all = benchmarks.get(sector_etf) if sector_etf else None
        if sector_close_all is None or len(sector_close_all) < 141:
            counts["missing_sector_context"] += 1
            continue

        df = prices.get(ticker)
        if df is None or len(df) < 170:
            continue
        frame = df.copy().reset_index(drop=True)
        stock_close_all = _close_series(frame)
        date_to_idx = {str(row["Date"])[:10]: i for i, row in frame.iterrows()}
        dates = ind["dates"]

        for i in range(62, len(dates)-1):
            rsi = ind["rsi"][i]
            dd60 = ind["dd60"][i]
            hist = ind["hist"][i]
            hist_prev = ind["hist"][i-1]
            vdec = ind["vdec"][i]
            atr = ind["atr"][i]
            if not (
                pd.notna(rsi) and float(rsi) < 40.0 and
                pd.notna(dd60) and float(dd60) <= -12.0 and
                pd.notna(hist) and pd.notna(hist_prev) and float(hist) > float(hist_prev) and
                bool(vdec) and pd.notna(atr) and float(atr) > 0
            ):
                continue
            counts["technical_candidates"] += 1

            signal_date = dates[i]
            cutoff = pd.Timestamp(signal_date).tz_localize(None).normalize()
            stock_cut = stock_close_all.loc[:cutoff]
            spy_cut = spy_close_all.loc[:cutoff]
            sector_cut = sector_close_all.loc[:cutoff]
            spy20 = _spy20(spy_cut)
            abnormal20, beta_spy, beta_sector, _ = _abnormal20(stock_cut, spy_cut, sector_cut)
            if not (np.isfinite(spy20) and np.isfinite(abnormal20)):
                continue
            counts["context_ready"] += 1
            if float(spy20) > 1.0 or float(abnormal20) > -10.0:
                continue

            row_idx = date_to_idx.get(signal_date)
            if row_idx is None or row_idx + 1 >= len(frame):
                continue
            nxt = frame.iloc[row_idx+1]
            entry = float(nxt["Open"])
            if not np.isfinite(entry) or entry <= 0:
                continue

            entry_date = str(nxt["Date"])[:10]
            entries.setdefault(entry_date, []).append({
                "ticker":ticker,
                "signal_date":signal_date,
                "entry_date":entry_date,
                "entry_price":entry,
                "atr":float(atr),
                "fund_score":0.0,
                "dd60":float(dd60),
                "rsi":float(rsi),
                "spy20":float(spy20),
                "abnormal20":float(abnormal20),
                "sector":sector,
                "sector_etf":sector_etf,
                "index_bucket":m.get("index_bucket"),
                "beta_spy":float(beta_spy) if np.isfinite(beta_spy) else np.nan,
                "beta_sector":float(beta_sector) if np.isfinite(beta_sector) else np.nan,
            })
            counts["prefund_full"] += 1
    return entries, counts


def passes_abc(sig):
    vals = (
        float(sig.get("range_pos60", np.nan)),
        float(sig.get("gap_pct", np.nan)),
        float(sig.get("volume_ratio_5_60", np.nan)),
    )
    if not all(np.isfinite(v) for v in vals):
        return False
    a,b,c = vals
    return (
        a >= A_RANGE_POS60_MIN and
        b >= B_GAP_PCT_MIN and
        C_VOL_RATIO_MIN <= c < C_VOL_RATIO_MAX
    )


def load_sp500_reference():
    csvs = sorted((Path("data/master")).glob("sp500_full_export_*.csv"))
    if not csvs:
        raise RuntimeError("No existe CSV maestro S&P500")
    df = pd.read_csv(csvs[-1])
    medians = _sector_medians(df)
    return df, medians


def add_liquidity(signals, prices):
    out = []
    for sig in signals:
        df = prices.get(sig["ticker"])
        if df is None:
            continue
        frame=df.copy().reset_index(drop=True)
        idxs=frame.index[frame["Date"].astype(str).str[:10] == sig["signal_date"]].tolist()
        if not idxs:
            continue
        i=idxs[0]
        start=max(0,i-19)
        px=pd.to_numeric(frame.loc[start:i,"Close"],errors="coerce")
        vol=pd.to_numeric(frame.loc[start:i,"Volume"],errors="coerce")
        adv=float((px*vol).mean()) if len(px) else np.nan
        sig=dict(sig)
        sig["adv20_usd"]=adv
        out.append(sig)
    return out


def run_group(label, signals, price_maps, all_dates):
    trades, skipped = build_episode_dataset(signals, price_maps)
    em = episode_metrics(trades)
    entries = to_entries_by_date(signals)
    pm, ptrades, eq = simulate_configuration(
        entries, price_maps, all_dates, TRADE_RISK_PCT, PORTFOLIO_CAP_PCT
    )
    by_index = {}
    for idx in ["SP500","SP400","SP600"]:
        s=[x for x in signals if x.get("index_bucket")==idx]
        t,_=build_episode_dataset(s, price_maps)
        by_index[idx]={"signals":len(s), **episode_metrics(t)}
    return {
        "label":label,
        "signals":len(signals),
        "unique_tickers":len(set(x["ticker"] for x in signals)),
        "episode_metrics":em,
        "portfolio_metrics":pm,
        "portfolio_trades":len(ptrades),
        "same_ticker_overlap_skipped":skipped,
        "by_index":by_index,
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    universe=build_universe()
    universe.to_csv(OUT_DIR/"sp1500_current_universe.csv",index=False)

    tickers=universe["ticker"].tolist()
    prices=fast_download_prices(tickers, years=YEARS)
    indicators=build_indicators(prices)
    benchmarks=download_context_benchmarks(YEARS)

    prefund_entries, prefund_counts=build_prefund_signals(
        prices, indicators, universe, benchmarks
    )

    pseudo_scores={
        row.ticker:{"sector":row.sector,"fund_score":0.0}
        for row in universe.itertuples()
    }
    enriched=enrich_signals(
        prefund_entries, prices, indicators, pseudo_scores, benchmarks
    )
    # Reattach index bucket because enrich_signals keeps **sig.
    abc_prefund=[s for s in enriched if passes_abc(s)]
    abc_prefund=add_liquidity(abc_prefund, prices)
    candidate_tickers=sorted(set(s["ticker"] for s in abc_prefund))

    master, medians=load_sp500_reference()
    scored=fast_candidate_fundamentals(candidate_tickers, universe, medians)
    scored.to_csv(OUT_DIR/"abc_candidate_fundamentals.csv",index=False)
    score_map={
        str(r["ticker"]):float(r["fund_score"])
        for _,r in scored.iterrows()
        if pd.notna(r.get("fund_score"))
    }

    abc_full=[]
    rejected_fund=0
    for sig in abc_prefund:
        sc=score_map.get(sig["ticker"],np.nan)
        if not np.isfinite(sc) or sc < 6.5:
            rejected_fund+=1
            continue
        x=dict(sig)
        x["fund_score"]=float(sc)
        abc_full.append(x)

    price_maps, all_dates=make_price_maps(prices)

    groups={
        "SP500_ABC":[s for s in abc_full if s.get("index_bucket")=="SP500"],
        "SP1500_ABC":abc_full,
        # Execution realism sensitivity check, not a new strategy rule.
        "SP1500_ABC_ADV20_20M":[s for s in abc_full if float(s.get("adv20_usd",0) or 0)>=20_000_000],
    }
    results={k:run_group(k,v,price_maps,all_dates) for k,v in groups.items()}

    # Calendar-year diagnostics.
    annual=[]
    for label,sigs in groups.items():
        for year in sorted(set(int(s["entry_date"][:4]) for s in sigs)):
            ys=[s for s in sigs if int(s["entry_date"][:4])==year]
            if not ys:
                continue
            t,_=build_episode_dataset(ys,price_maps)
            m=episode_metrics(t)
            annual.append({"group":label,"year":year,"signals":len(ys),**m})
    pd.DataFrame(annual).to_csv(OUT_DIR/"abc_expanded_annual.csv",index=False)

    # Signal-level export.
    pd.DataFrame(abc_full).to_csv(OUT_DIR/"abc_expanded_signals.csv",index=False)

    report={
        "generated_at":datetime.utcnow().isoformat()+"Z",
        "strategy_version":STRATEGY_VERSION,
        "model_version":MODEL_VERSION,
        "methodology":{
            "universe":"Current S&P Composite 1500 = S&P500+S&P400+S&P600",
            "years":YEARS,
            "abc":{
                "A":f"range_pos60 >= {A_RANGE_POS60_MIN}",
                "B":f"gap_pct >= {B_GAP_PCT_MIN}",
                "C":f"{C_VOL_RATIO_MIN} <= volume_ratio_5_60 < {C_VOL_RATIO_MAX}",
            },
            "fund_gate":"fund_score >= 6.5",
            "fund_reference":"New names scored against current S&P500 sector medians",
            "portfolio":{"trade_risk_pct":TRADE_RISK_PCT,"cap_pct":PORTFOLIO_CAP_PCT,"max_positions":3},
            "survivorship_bias":"Uses current index constituents, same known limitation as existing S&P500 historical proxy.",
            "adv20_20m_group":"Sensitivity check only; not part of frozen ABC.",
        },
        "universe_counts":universe["index_bucket"].value_counts().to_dict(),
        "prices_downloaded":len(prices),
        "prefund_counts":prefund_counts,
        "abc_prefund_signals":len(abc_prefund),
        "abc_candidate_unique_tickers":len(candidate_tickers),
        "fund_scored_tickers":len(scored),
        "fund_rejected_signals":rejected_fund,
        "abc_full_signals":len(abc_full),
        "results":results,
    }
    (OUT_DIR/"abc_expanded_report.json").write_text(
        json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding="utf-8"
    )

    print("\nRESULTADOS")
    for k,v in results.items():
        em=v["episode_metrics"]; pm=v["portfolio_metrics"]
        print(
            f"{k}: signals={v['signals']} episodes={em.get('n')} "
            f"WR={em.get('win_rate',np.nan):.2f}% PF={em.get('profit_factor',np.nan):.3f} "
            f"avgR={em.get('avg_r',np.nan):+.3f} "
            f"return={pm.get('total_return_pct',np.nan):+.1f}% "
            f"CAGR={pm.get('cagr_pct',np.nan):.2f}% MDD={pm.get('max_drawdown_pct',np.nan):.2f}%"
        )


if __name__=="__main__":
    main()
