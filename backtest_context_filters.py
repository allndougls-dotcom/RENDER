"""Portfolio-level validation of contextual filters discovered in SIDI.

Unlike post-hoc trade slicing, filters are applied to the SIGNAL MAP before
portfolio simulation. This allows freed position slots to be filled by other
eligible signals and therefore measures the actual portfolio effect.

Thresholds are deliberately rounded, interpretable values after the discovery
pass; no fine grid optimisation is performed here.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

import backtest as bt
import backtest_experiments as exp
import validate_sidi_candidate as val
import historical_membership as membership
import analyze_signal_context_features as feat

START = "2024-08-11"
END = "2026-09-10"
DOWNLOAD_START = "2023-01-01"
COST_BPS = 10

CANDIDATE = exp.Experiment(
    "CONTEXT_FILTER_VALIDATION",
    target_mode="atr", atr_mult=0.75,
    stop_mode="fixed", stop_pct=0.05,
    time_stop=7,
    description="DD12 / TP0.75ATR / SL5 / T7 / risk1.5",
)

WINDOWS = [
    ("H1_DISCOVERY", "2024-08-11", "2025-08-31"),
    ("H2_TEMPORAL", "2025-09-01", "2026-09-10"),
]


def build_context(signal_map, prices_idx, sectors, refs, market_breadth, sector_breadth):
    spy = refs["SPY"]
    vix = refs["^VIX"]
    vix_series = pd.to_numeric(vix["Close"], errors="coerce") if not vix.empty else pd.Series(dtype=float)
    out = {}
    total = sum(len(v) for v in signal_map.values())
    done = 0
    for ticker, sigs in signal_map.items():
        sdf = prices_idx.get(ticker, pd.DataFrame())
        sector = sectors.get(ticker, "Unknown")
        etf_sym = feat.SECTOR_ETF.get(sector)
        edf = refs.get(etf_sym, pd.DataFrame()) if etf_sym else pd.DataFrame()
        for d, sig in sigs.items():
            ts = pd.Timestamp(d)
            spy20 = feat.ret_n(spy, d, 20)
            _, vol_ratio = feat.volume_features(sdf, d)
            rec = {
                "spy20": spy20*100 if pd.notna(spy20) else np.nan,
                "vix_pct": feat.percentile_asof(vix_series, d, 252) if len(vix_series) else np.nan,
                "sector_breadth": sector_breadth.get((ts, sector), np.nan),
                "abnormal20": feat.beta_adjusted_abnormal(sdf, spy, edf, d),
                "vol_ratio": vol_ratio,
            }
            out[(ticker,d)] = rec
            done += 1
            if done % 250 == 0:
                print(f"  signal context {done}/{total}")
    return out


def subset_signal_map(signal_map, ctx, predicate):
    out = {}
    kept = 0
    for ticker, sigs in signal_map.items():
        for d, sig in sigs.items():
            rec = ctx.get((ticker,d), {})
            try:
                ok = bool(predicate(rec))
            except Exception:
                ok = False
            if ok:
                out.setdefault(ticker,{})[d] = sig
                kept += 1
    return out, kept


def present(x):
    return x is not None and pd.notna(x)


def rules():
    return {
        "BASE": lambda r: True,
        "SPY20_LE_1": lambda r: present(r.get("spy20")) and r["spy20"] <= 1.0,
        "VIX_GE_40": lambda r: present(r.get("vix_pct")) and r["vix_pct"] >= 40.0,
        "SECTOR_BREADTH_LE_40": lambda r: present(r.get("sector_breadth")) and r["sector_breadth"] <= 40.0,
        "ABNORMAL20_LE_M10": lambda r: present(r.get("abnormal20")) and r["abnormal20"] <= -10.0,
        "VOL_RATIO_LE_1": lambda r: present(r.get("vol_ratio")) and r["vol_ratio"] <= 1.0,
        "SPY1_AND_VIX40": lambda r: present(r.get("spy20")) and present(r.get("vix_pct")) and r["spy20"] <= 1.0 and r["vix_pct"] >= 40.0,
        "SPY1_AND_ABN_M10": lambda r: present(r.get("spy20")) and present(r.get("abnormal20")) and r["spy20"] <= 1.0 and r["abnormal20"] <= -10.0,
        "VIX40_AND_ABN_M10": lambda r: present(r.get("vix_pct")) and present(r.get("abnormal20")) and r["vix_pct"] >= 40.0 and r["abnormal20"] <= -10.0,
        "SPY1_VIX40_ABN_M10": lambda r: present(r.get("spy20")) and present(r.get("vix_pct")) and present(r.get("abnormal20")) and r["spy20"] <= 1.0 and r["vix_pct"] >= 40.0 and r["abnormal20"] <= -10.0,
    }


def main():
    print("="*112)
    print("SIDI PORTFOLIO BACKTEST — CONTEXT FILTER VALIDATION")
    print("="*112)
    print("Rounded thresholds only; no fine optimisation. Filters applied before slot allocation.\n")

    _, sectors = feat.load_master()
    tickers = bt.load_tickers()
    fund_scores = bt.load_fundamental_scores()
    added = membership.load_date_added()
    prices = bt.download_prices(tickers, start_date=DOWNLOAD_START, end_date=END)
    indicators = bt.build_indicators(prices)
    prices_idx = {t: feat.to_indexed(d) for t,d in prices.items()}

    spec = exp.signal_variant(12.0, False)
    raw, nraw = bt.build_signal_map(indicators, spec, spy_dict={}, date_from=START, date_to=END,
                                    fund_scores=fund_scores, sector_regime=None)
    base_map, member_stats = membership.filter_signal_map(raw, added)
    print(f"Signals raw={nraw}; membership-kept={member_stats['kept']}; removed={member_stats['removed_pre_membership']}")

    refs = {s: feat.download_reference(s) for s in sorted(set(feat.SECTOR_ETF.values()) | {"SPY","^VIX"})}
    print("Computing breadth...")
    market_breadth, sector_breadth = feat.breadth_maps(prices_idx, sectors, added)
    print("Computing context for all eligible signals...")
    ctx = build_context(base_map, prices_idx, sectors, refs, market_breadth, sector_breadth)

    full_rows=[]; window_rows=[]
    maps={}
    for name,pred in rules().items():
        smap,n = subset_signal_map(base_map,ctx,pred)
        maps[name]=smap
        st,_ = val.run_sim(smap, prices, indicators, fund_scores, CANDIDATE, START, END, cost_bps=COST_BPS)
        st.update({"filter":name,"signals_kept":n,"signals_base":member_stats["kept"],"signal_keep_pct":100*n/member_stats["kept"] if member_stats["kept"] else 0})
        full_rows.append(st)
        print(f"{name:<24} sig={n:4d} trades={st['trades']:3d} WR={st['win_rate']:6.2f}% PF={st['profit_factor']:5.2f} Ret={st['total_return']:8.2f}% MDD={st['max_drawdown_mtm']:7.2f}% Days={st['avg_days']:4.2f}")

    print("\nTEMPORAL WINDOWS — capital reset")
    for wname,ws,we in WINDOWS:
        for name,smap in maps.items():
            st,_=val.run_sim(smap, prices, indicators, fund_scores, CANDIDATE, ws, we, cost_bps=COST_BPS)
            st.update({"window":wname,"filter":name})
            window_rows.append(st)
            print(f"{wname:<12} {name:<24} trades={st['trades']:3d} WR={st['win_rate']:6.2f}% PF={st['profit_factor']:5.2f} Ret={st['total_return']:7.2f}% MDD={st['max_drawdown_mtm']:7.2f}%")

    pd.DataFrame(full_rows).drop(columns=["config"],errors="ignore").to_csv("sidi_context_filters_full.csv",index=False)
    pd.DataFrame(window_rows).drop(columns=["config"],errors="ignore").to_csv("sidi_context_filters_windows.csv",index=False)
    Path("sidi_context_filters_results.json").write_text(json.dumps({
        "generated_at":datetime.utcnow().isoformat()+"Z",
        "period":[START,END],"cost_bps_rt":COST_BPS,"membership":member_stats,
        "thresholds":{"spy20_max_pct":1.0,"vix_percentile_min":40.0,"sector_breadth_max_pct":40.0,"abnormal20_max_pct":-10.0,"volume_ratio_max":1.0},
        "note":"Discovery validation with current fund_score proxy. Must confirm any promoted rule with PIT/OOS data.",
        "full":full_rows,"windows":window_rows,
    },indent=2,ensure_ascii=False,default=str),encoding="utf-8")
    print("\nSaved portfolio-level context filter results.")

if __name__ == "__main__":
    main()
