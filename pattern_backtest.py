#!/usr/bin/env python3
"""
SIDI_INTRADAY_V2 · Pattern Discovery Backtest

Objetivo:
- estudiar TODAS las FULL como episodios independientes por ticker;
- usar exclusivamente variables conocidas antes de la entrada;
- descubrir filtros en el 60% inicial del histórico;
- validarlos en el 20% siguiente;
- mantener el 20% final como test OOS no usado para descubrir thresholds.

No modifica la estrategia ni main. Es un análisis experimental de patrones.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import build_indicators, download_prices, load_fundamental_scores, load_tickers
from modules.ingesta.sidi_context import SECTOR_ETF_MAP, _abnormal20, _close_series, _spy20
from risk_matrix_backtest import (
    COST_PCT_RT,
    STOP_PCT,
    TIME_STOP_SESSIONS,
    TP1_ATR_MULT,
    TP2_ATR_MULT,
    STRATEGY_VERSION,
    build_signals,
    download_context_benchmarks,
    make_price_maps,
    norm_ticker,
)

MODEL_VERSION = "SIDI_PATTERN_DISCOVERY_V1"
OUT_DIR_DEFAULT = "data/pattern_analysis"

# fund_score se conserva para diagnóstico, pero NO se usa para proponer filtros:
# en este backtest es el score actual aplicado retroactivamente.
NUMERIC_FEATURES = [
    "rsi",
    "dd60",
    "spy20",
    "abnormal20",
    "atr_pct",
    "gap_pct",
    "ret5_pct",
    "ret10_pct",
    "ret20_pct",
    "volume_ratio_5_60",
    "macd_delta_atr",
    "price_vs_ma20_pct",
    "price_vs_ma50_pct",
    "price_vs_ma200_pct",
    "range_pos60",
    "down_days5",
    "vol20_ann_pct",
    "sector20_pct",
    "sector_minus_spy20_pct",
    "beta_spy",
    "beta_sector",
    "rsi_change5",
    "dd60_change5",
]

CATEGORICAL_FEATURES = ["sector", "entry_weekday"]


def safe_float(value, default=np.nan):
    try:
        out = float(value)
        return out if np.isfinite(out) else default
    except Exception:
        return default


def pct_return(values, lookback):
    if len(values) <= lookback:
        return np.nan
    first = safe_float(values[-lookback - 1])
    last = safe_float(values[-1])
    if not np.isfinite(first) or first <= 0 or not np.isfinite(last):
        return np.nan
    return (last / first - 1.0) * 100.0


def enrich_signals(entries_by_date, prices, indicators, fund_scores, benchmarks):
    """Añade variables PRE-ENTRY. Ninguna usa datos posteriores a la entrada."""
    enriched = []
    for entry_date in sorted(entries_by_date):
        for sig in entries_by_date[entry_date]:
            ticker = sig["ticker"]
            df = prices.get(ticker)
            ind = indicators.get(ticker)
            if df is None or ind is None:
                continue

            signal_date = sig["signal_date"]
            dates = ind["dates"]
            try:
                i = dates.index(signal_date)
            except ValueError:
                continue
            if i < 60:
                continue

            close = np.asarray(ind["close"], dtype=float)
            atr = safe_float(sig["atr"])
            signal_close = safe_float(close[i])
            if not np.isfinite(signal_close) or signal_close <= 0:
                continue

            frame = df.copy().reset_index(drop=True)
            date_to_idx = {str(row["Date"])[:10]: j for j, row in frame.iterrows()}
            row_i = date_to_idx.get(signal_date)
            if row_i is None:
                continue

            closes = pd.to_numeric(frame.loc[:row_i, "Close"], errors="coerce").to_numpy(dtype=float)
            volumes = pd.to_numeric(frame.loc[:row_i, "Volume"], errors="coerce").to_numpy(dtype=float)
            rets = pd.Series(closes).pct_change()

            ma20 = np.nanmean(closes[-20:]) if len(closes) >= 20 else np.nan
            ma50 = np.nanmean(closes[-50:]) if len(closes) >= 50 else np.nan
            ma200 = np.nanmean(closes[-200:]) if len(closes) >= 200 else np.nan
            low60 = np.nanmin(closes[-60:]) if len(closes) >= 60 else np.nan
            high60 = np.nanmax(closes[-60:]) if len(closes) >= 60 else np.nan
            range_pos60 = (
                (signal_close - low60) / (high60 - low60)
                if np.isfinite(low60) and np.isfinite(high60) and high60 > low60
                else np.nan
            )
            down_days5 = (
                int((pd.Series(closes).diff().tail(5) < 0).sum())
                if len(closes) >= 6 else np.nan
            )
            vol20_ann = (
                float(rets.tail(20).std(ddof=0) * np.sqrt(252) * 100.0)
                if len(rets.dropna()) >= 20 else np.nan
            )
            vol5 = np.nanmean(volumes[-5:]) if len(volumes) >= 5 else np.nan
            vol60 = np.nanmean(volumes[-60:]) if len(volumes) >= 60 else np.nan
            volume_ratio = vol5 / vol60 if np.isfinite(vol60) and vol60 > 0 else np.nan

            hist = np.asarray(ind["hist"], dtype=float)
            macd_delta = safe_float(hist[i] - hist[i - 1])
            macd_delta_atr = macd_delta / atr if np.isfinite(atr) and atr > 0 else np.nan

            rsi_arr = np.asarray(ind["rsi"], dtype=float)
            dd_arr = np.asarray(ind["dd60"], dtype=float)
            rsi_change5 = (
                safe_float(rsi_arr[i] - rsi_arr[i - 5]) if i >= 5 else np.nan
            )
            dd_change5 = (
                safe_float(dd_arr[i] - dd_arr[i - 5]) if i >= 5 else np.nan
            )

            fs = fund_scores.get(ticker, {})
            sector = str(sig.get("sector") or fs.get("sector") or "Unknown")
            sector_etf = SECTOR_ETF_MAP.get(sector)
            cutoff = pd.Timestamp(signal_date).tz_localize(None).normalize()
            stock_close = _close_series(frame).loc[:cutoff]
            spy_close = benchmarks["SPY"].loc[:cutoff]
            sector_close = (
                benchmarks.get(sector_etf, pd.Series(dtype=float)).loc[:cutoff]
                if sector_etf else pd.Series(dtype=float)
            )
            abnormal20, beta_spy, beta_sector, _ = (
                _abnormal20(stock_close, spy_close, sector_close)
                if len(sector_close) else (np.nan, np.nan, np.nan, 0)
            )
            sector20 = _spy20(sector_close) if len(sector_close) else np.nan
            spy20 = safe_float(sig["spy20"])
            entry_price = safe_float(sig["entry_price"])
            gap_pct = (entry_price / signal_close - 1.0) * 100.0

            enriched.append({
                **sig,
                "signal_close": signal_close,
                "atr_pct": atr / signal_close * 100.0 if atr > 0 else np.nan,
                "gap_pct": gap_pct,
                "ret5_pct": pct_return(closes, 5),
                "ret10_pct": pct_return(closes, 10),
                "ret20_pct": pct_return(closes, 20),
                "volume_ratio_5_60": volume_ratio,
                "macd_delta_atr": macd_delta_atr,
                "price_vs_ma20_pct": (signal_close / ma20 - 1.0) * 100.0 if ma20 > 0 else np.nan,
                "price_vs_ma50_pct": (signal_close / ma50 - 1.0) * 100.0 if ma50 > 0 else np.nan,
                "price_vs_ma200_pct": (signal_close / ma200 - 1.0) * 100.0 if ma200 > 0 else np.nan,
                "range_pos60": range_pos60,
                "down_days5": down_days5,
                "vol20_ann_pct": vol20_ann,
                "sector20_pct": sector20,
                "sector_minus_spy20_pct": sector20 - spy20 if np.isfinite(sector20) else np.nan,
                "beta_spy": beta_spy,
                "beta_sector": beta_sector,
                "rsi_change5": rsi_change5,
                "dd60_change5": dd_change5,
                "fund_score": safe_float(fs.get("fund_score")),
                "entry_weekday": pd.Timestamp(entry_date).day_name(),
                "entry_month": pd.Timestamp(entry_date).month,
                # sanity check: debe coincidir con build_signals salvo redondeos.
                "abnormal20_recomputed": abnormal20,
            })
    return enriched


def _gross_r_for_exit(entry, risk_per_share, exit_price, tp1_hit, tp1_price):
    exit_r = (float(exit_price) - entry) / risk_per_share
    if tp1_hit:
        tp1_r = (tp1_price - entry) / risk_per_share
        return 0.5 * tp1_r + 0.5 * exit_r
    return exit_r


def simulate_one_trade(sig, price_maps):
    """Simula una FULL con las reglas congeladas, sin límites de cartera."""
    ticker = sig["ticker"]
    entry_date = sig["entry_date"]
    entry = safe_float(sig["entry_price"])
    atr = safe_float(sig["atr"])
    if not np.isfinite(entry) or entry <= 0 or not np.isfinite(atr) or atr <= 0:
        return None

    rows = price_maps.get(ticker, {})
    dates = [d for d in sorted(rows) if d >= entry_date]
    if not dates:
        return None

    risk_per_share = entry * STOP_PCT
    stop = entry * (1.0 - STOP_PCT)
    tp1 = entry + TP1_ATR_MULT * atr
    tp2 = entry + TP2_ATR_MULT * atr
    costs_r = COST_PCT_RT / STOP_PCT

    tp1_hit = False
    mfe_r = 0.0
    mae_r = 0.0
    sessions = 0
    exit_date = None
    exit_price = None
    exit_reason = None
    gross_r = None

    for j, date in enumerate(dates):
        row = rows[date]
        sessions += 1
        high_r = (row["High"] - entry) / risk_per_share
        low_r = (row["Low"] - entry) / risk_per_share
        mfe_r = max(mfe_r, high_r)
        mae_r = min(mae_r, low_r)

        effective_stop = entry if tp1_hit else stop

        # Gap stop solo a partir de la sesión posterior a la entrada.
        if j > 0 and row["Open"] <= effective_stop:
            exit_price = row["Open"]
            exit_reason = "GAP_BE" if tp1_hit else "GAP_SL"
            gross_r = _gross_r_for_exit(entry, risk_per_share, exit_price, tp1_hit, tp1)
            exit_date = date
            break

        # Conflicto intradía conservador: SL/BE primero.
        if row["Low"] <= effective_stop:
            exit_price = effective_stop
            exit_reason = "BE" if tp1_hit else "SL"
            gross_r = _gross_r_for_exit(entry, risk_per_share, exit_price, tp1_hit, tp1)
            exit_date = date
            break

        if not tp1_hit and row["High"] >= tp1:
            tp1_hit = True
            # Igual que shadow_intraday_v2: tras TP1, si el mismo bar toca entrada -> TP1_BE.
            if row["Low"] <= entry:
                exit_price = entry
                exit_reason = "TP1_BE"
                gross_r = _gross_r_for_exit(entry, risk_per_share, exit_price, True, tp1)
                exit_date = date
                break

        if tp1_hit and row["High"] >= tp2:
            exit_price = tp2
            exit_reason = "TP2"
            gross_r = _gross_r_for_exit(entry, risk_per_share, exit_price, True, tp1)
            exit_date = date
            break

        if sessions >= TIME_STOP_SESSIONS:
            exit_price = row["Close"]
            exit_reason = "T7"
            gross_r = _gross_r_for_exit(entry, risk_per_share, exit_price, tp1_hit, tp1)
            exit_date = date
            break

    if gross_r is None:
        date = dates[-1]
        row = rows[date]
        exit_price = row["Close"]
        exit_reason = "END_OF_PERIOD"
        gross_r = _gross_r_for_exit(entry, risk_per_share, exit_price, tp1_hit, tp1)
        exit_date = date

    r_net = gross_r - costs_r
    return {
        **sig,
        "exit_date": exit_date,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "sessions": sessions,
        "tp1_hit": bool(tp1_hit),
        "gross_r": gross_r,
        "r_multiple_net": r_net,
        "pnl_pct_net": r_net * STOP_PCT * 100.0,
        "win": bool(r_net > 0),
        "hard_loss": bool(exit_reason in {"SL", "GAP_SL"}),
        "gap_loss": bool(exit_reason == "GAP_SL"),
        "mfe_r": mfe_r,
        "mae_r": mae_r,
    }


def build_episode_dataset(enriched_signals, price_maps):
    """
    Evita contar el mismo ticker cada día mientras sigue dentro del mismo episodio.
    No hay límite global de posiciones: solo se bloquea una nueva entrada del MISMO
    ticker hasta que el episodio anterior haya cerrado.
    """
    signals = sorted(enriched_signals, key=lambda x: (x["entry_date"], x["ticker"], x["signal_date"]))
    last_exit_by_ticker = {}
    trades = []
    skipped_overlap_same_ticker = 0
    for sig in signals:
        last_exit = last_exit_by_ticker.get(sig["ticker"])
        if last_exit and sig["entry_date"] <= last_exit:
            skipped_overlap_same_ticker += 1
            continue
        trade = simulate_one_trade(sig, price_maps)
        if trade is None:
            continue
        trades.append(trade)
        last_exit_by_ticker[sig["ticker"]] = trade["exit_date"]
    return pd.DataFrame(trades), skipped_overlap_same_ticker


def metrics(df):
    if df is None or len(df) == 0:
        return {
            "n": 0, "win_rate": np.nan, "avg_r": np.nan, "median_r": np.nan,
            "profit_factor": np.nan, "p10_r": np.nan, "hard_loss_rate": np.nan,
            "gap_loss_rate": np.nan,
        }
    r = pd.to_numeric(df["r_multiple_net"], errors="coerce").dropna()
    if len(r) == 0:
        return {"n": 0}
    gp = float(r[r > 0].sum())
    gl = float(-r[r < 0].sum())
    pf = gp / gl if gl > 0 else 999.0
    return {
        "n": int(len(r)),
        "win_rate": float((r > 0).mean() * 100.0),
        "avg_r": float(r.mean()),
        "median_r": float(r.median()),
        "profit_factor": float(pf),
        "p10_r": float(r.quantile(0.10)),
        "hard_loss_rate": float(df.loc[r.index, "hard_loss"].mean() * 100.0),
        "gap_loss_rate": float(df.loc[r.index, "gap_loss"].mean() * 100.0),
    }


def assign_splits(df):
    df = df.sort_values(["entry_date", "ticker"]).reset_index(drop=True)
    dates = pd.to_datetime(df["entry_date"])
    unique_dates = np.array(sorted(dates.dt.normalize().unique()))
    if len(unique_dates) < 10:
        raise RuntimeError("Muy pocas fechas para split temporal")
    d60 = unique_dates[min(int(len(unique_dates) * 0.60), len(unique_dates) - 1)]
    d80 = unique_dates[min(int(len(unique_dates) * 0.80), len(unique_dates) - 1)]
    df["split"] = np.where(
        dates.dt.normalize() < d60,
        "train",
        np.where(dates.dt.normalize() < d80, "validation", "test"),
    )
    return df, str(pd.Timestamp(d60).date()), str(pd.Timestamp(d80).date())


def apply_candidate(df, cand):
    kind = cand["kind"]
    if kind == "numeric":
        s = pd.to_numeric(df[cand["feature"]], errors="coerce")
        if cand["op"] == "<=":
            return s <= float(cand["threshold"])
        return s >= float(cand["threshold"])
    if kind == "categorical_exclude":
        return df[cand["feature"]].astype(str) != str(cand["value"])
    if kind == "categorical_include":
        return df[cand["feature"]].astype(str) == str(cand["value"])
    if kind == "and":
        left = apply_candidate(df, cand["left"])
        right = apply_candidate(df, cand["right"])
        return left & right
    raise ValueError(kind)


def evaluate_candidate(df, cand, split_name):
    part = df[df["split"] == split_name]
    base = metrics(part)
    mask = apply_candidate(part, cand).fillna(False)
    kept = part[mask]
    removed = part[~mask]
    km = metrics(kept)
    rm = metrics(removed)
    return {
        "split": split_name,
        "baseline_n": base["n"],
        "baseline_avg_r": base["avg_r"],
        "baseline_pf": base["profit_factor"],
        "baseline_wr": base["win_rate"],
        "kept_n": km["n"],
        "keep_rate": km["n"] / base["n"] if base["n"] else 0.0,
        "kept_avg_r": km["avg_r"],
        "kept_pf": km["profit_factor"],
        "kept_wr": km["win_rate"],
        "kept_p10_r": km["p10_r"],
        "removed_n": rm["n"],
        "removed_avg_r": rm["avg_r"],
        "removed_pf": rm["profit_factor"],
        "delta_avg_r": km["avg_r"] - base["avg_r"] if km["n"] else np.nan,
        "delta_wr": km["win_rate"] - base["win_rate"] if km["n"] else np.nan,
        "delta_pf": km["profit_factor"] - base["profit_factor"] if km["n"] else np.nan,
    }


def candidate_label(cand):
    if cand["kind"] == "numeric":
        return f'{cand["feature"]} {cand["op"]} {float(cand["threshold"]):.4g}'
    if cand["kind"] == "categorical_exclude":
        return f'{cand["feature"]} != {cand["value"]}'
    if cand["kind"] == "categorical_include":
        return f'{cand["feature"]} == {cand["value"]}'
    if cand["kind"] == "and":
        return f'({candidate_label(cand["left"])}) AND ({candidate_label(cand["right"])})'
    return str(cand)


def discover_univariate(train):
    base = metrics(train)
    out = []
    for feature in NUMERIC_FEATURES:
        if feature not in train.columns:
            continue
        s = pd.to_numeric(train[feature], errors="coerce").dropna()
        if len(s) < 60 or s.nunique() < 6:
            continue
        thresholds = sorted(set(float(x) for x in s.quantile(np.arange(0.10, 0.91, 0.10)).dropna()))
        for threshold in thresholds:
            for op in ("<=", ">="):
                cand = {"kind": "numeric", "feature": feature, "op": op, "threshold": threshold}
                mask = apply_candidate(train, cand).fillna(False)
                kept, removed = train[mask], train[~mask]
                km, rm = metrics(kept), metrics(removed)
                keep_rate = km["n"] / base["n"] if base["n"] else 0
                if km["n"] < 40 or rm["n"] < 18 or not (0.40 <= keep_rate <= 0.90):
                    continue
                out.append({
                    "candidate": cand,
                    "label": candidate_label(cand),
                    "train_keep_rate": keep_rate,
                    "train_kept_n": km["n"],
                    "train_avg_r": km["avg_r"],
                    "train_pf": km["profit_factor"],
                    "train_wr": km["win_rate"],
                    "train_removed_avg_r": rm["avg_r"],
                    "train_delta_avg_r": km["avg_r"] - base["avg_r"],
                    "train_delta_pf": km["profit_factor"] - base["profit_factor"],
                    "train_delta_wr": km["win_rate"] - base["win_rate"],
                })

    for feature in CATEGORICAL_FEATURES:
        if feature not in train.columns:
            continue
        counts = train[feature].astype(str).value_counts()
        for value, count in counts.items():
            if count < 15:
                continue
            for kind in ("categorical_exclude", "categorical_include"):
                cand = {"kind": kind, "feature": feature, "value": value}
                mask = apply_candidate(train, cand).fillna(False)
                kept, removed = train[mask], train[~mask]
                km, rm = metrics(kept), metrics(removed)
                keep_rate = km["n"] / base["n"] if base["n"] else 0
                if km["n"] < 40 or rm["n"] < 15 or not (0.40 <= keep_rate <= 0.90):
                    continue
                out.append({
                    "candidate": cand,
                    "label": candidate_label(cand),
                    "train_keep_rate": keep_rate,
                    "train_kept_n": km["n"],
                    "train_avg_r": km["avg_r"],
                    "train_pf": km["profit_factor"],
                    "train_wr": km["win_rate"],
                    "train_removed_avg_r": rm["avg_r"],
                    "train_delta_avg_r": km["avg_r"] - base["avg_r"],
                    "train_delta_pf": km["profit_factor"] - base["profit_factor"],
                    "train_delta_wr": km["win_rate"] - base["win_rate"],
                })

    # Mejor threshold por feature/dirección para evitar veinte variantes casi idénticas.
    frame = pd.DataFrame(out)
    if frame.empty:
        return []
    frame["feature_key"] = frame["candidate"].apply(
        lambda c: f'{c.get("feature")}::{c.get("op", c.get("kind"))}'
    )
    frame = frame.sort_values(
        ["train_delta_avg_r", "train_delta_pf", "train_keep_rate"],
        ascending=[False, False, False],
    )
    frame = frame.groupby("feature_key", as_index=False).head(1)
    return frame.drop(columns=["feature_key"]).to_dict("records")


def bootstrap_kept_vs_removed(df, cand, n_boot=2000, seed=42):
    mask = apply_candidate(df, cand).fillna(False)
    kept = pd.to_numeric(df.loc[mask, "r_multiple_net"], errors="coerce").dropna().to_numpy()
    removed = pd.to_numeric(df.loc[~mask, "r_multiple_net"], errors="coerce").dropna().to_numpy()
    if len(kept) < 10 or len(removed) < 10:
        return np.nan
    rng = np.random.default_rng(seed)
    wins = 0
    for _ in range(n_boot):
        k = rng.choice(kept, size=len(kept), replace=True).mean()
        r = rng.choice(removed, size=len(removed), replace=True).mean()
        wins += k > r
    return wins / n_boot


def select_patterns(df):
    train = df[df["split"] == "train"]
    validation = df[df["split"] == "validation"]
    test = df[df["split"] == "test"]

    discovered = discover_univariate(train)
    rows = []
    for item in discovered:
        cand = item["candidate"]
        val = evaluate_candidate(df, cand, "validation")
        tst = evaluate_candidate(df, cand, "test")
        row = {**item}
        row.update({f"val_{k}": v for k, v in val.items() if k != "split"})
        row.update({f"test_{k}": v for k, v in tst.items() if k != "split"})
        row["min_oos_delta_r"] = min(
            val.get("delta_avg_r", -999) if np.isfinite(val.get("delta_avg_r", np.nan)) else -999,
            tst.get("delta_avg_r", -999) if np.isfinite(tst.get("delta_avg_r", np.nan)) else -999,
        )
        row["robust_single"] = bool(
            item["train_delta_avg_r"] > 0
            and val["kept_n"] >= 20 and tst["kept_n"] >= 20
            and val["delta_avg_r"] > 0 and tst["delta_avg_r"] > 0
            and val["kept_pf"] >= val["baseline_pf"]
            and tst["kept_pf"] >= tst["baseline_pf"]
            and val["keep_rate"] >= 0.35 and tst["keep_rate"] >= 0.35
        )
        rows.append(row)

    # Para interacciones usamos solo candidatos que ya muestran señal en validation.
    val_positive = [
        row for row in rows
        if row["train_delta_avg_r"] > 0
        and row.get("val_delta_avg_r", -999) > 0
        and row.get("val_kept_n", 0) >= 20
        and row.get("val_keep_rate", 0) >= 0.35
    ]
    val_positive = sorted(
        val_positive,
        key=lambda x: (x.get("val_delta_avg_r", -999), x.get("val_delta_pf", -999)),
        reverse=True,
    )[:8]

    interactions = []
    for left, right in itertools.combinations(val_positive, 2):
        lc, rc = left["candidate"], right["candidate"]
        # No combinar dos thresholds del mismo feature.
        if lc.get("feature") == rc.get("feature"):
            continue
        cand = {"kind": "and", "left": lc, "right": rc}
        tr = evaluate_candidate(df, cand, "train")
        val = evaluate_candidate(df, cand, "validation")
        tst = evaluate_candidate(df, cand, "test")
        if min(tr["kept_n"], val["kept_n"], tst["kept_n"]) < 18:
            continue
        if min(tr["keep_rate"], val["keep_rate"], tst["keep_rate"]) < 0.30:
            continue
        interactions.append({
            "candidate": cand,
            "label": candidate_label(cand),
            "train": tr,
            "validation": val,
            "test": tst,
            "robust_interaction": bool(
                tr["delta_avg_r"] > 0 and val["delta_avg_r"] > 0 and tst["delta_avg_r"] > 0
                and val["kept_pf"] >= val["baseline_pf"]
                and tst["kept_pf"] >= tst["baseline_pf"]
            ),
            "min_oos_delta_r": min(val["delta_avg_r"], tst["delta_avg_r"]),
        })

    # Singles robustos: bootstrap sobre OOS conjunto, después de haber congelado threshold en TRAIN.
    oos = df[df["split"].isin(["validation", "test"])]
    for row in rows:
        if row["robust_single"]:
            row["oos_bootstrap_prob_kept_gt_removed"] = bootstrap_kept_vs_removed(
                oos, row["candidate"]
            )
        else:
            row["oos_bootstrap_prob_kept_gt_removed"] = np.nan

    rows = sorted(
        rows,
        key=lambda x: (
            bool(x["robust_single"]),
            x.get("min_oos_delta_r", -999),
            x.get("train_delta_avg_r", -999),
        ),
        reverse=True,
    )
    interactions = sorted(
        interactions,
        key=lambda x: (bool(x["robust_interaction"]), x["min_oos_delta_r"]),
        reverse=True,
    )
    return rows, interactions


def bucket_diagnostics(df):
    rows = []
    for feature in NUMERIC_FEATURES + ["fund_score"]:
        if feature not in df.columns:
            continue
        s = pd.to_numeric(df[feature], errors="coerce")
        valid = df[s.notna()].copy()
        if len(valid) < 40 or s.nunique() < 5:
            continue
        try:
            valid["bucket"] = pd.qcut(
                pd.to_numeric(valid[feature], errors="coerce"),
                q=5,
                duplicates="drop",
            )
        except Exception:
            continue
        for bucket, sub in valid.groupby("bucket", observed=True):
            m = metrics(sub)
            rows.append({
                "feature": feature,
                "bucket": str(bucket),
                **m,
            })
    return pd.DataFrame(rows)


def categorical_diagnostics(df):
    rows = []
    for feature in ["sector", "entry_weekday", "entry_month", "exit_reason"]:
        if feature not in df.columns:
            continue
        for value, sub in df.groupby(feature, dropna=False):
            if len(sub) < 8:
                continue
            rows.append({
                "feature": feature,
                "value": str(value),
                **metrics(sub),
            })
    return pd.DataFrame(rows)


def json_clean(obj):
    if isinstance(obj, dict):
        return {str(k): json_clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if not np.isfinite(obj) else float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    return obj


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=int, default=4)
    p.add_argument("--out-dir", default=OUT_DIR_DEFAULT)
    args = p.parse_args()

    print("=" * 84)
    print("SIDI_INTRADAY_V2 · PATTERN DISCOVERY")
    print(f"Modelo: {MODEL_VERSION}")
    print("=" * 84)

    all_tickers = [norm_ticker(t) for t in load_tickers()]
    fund_scores = {norm_ticker(t): v for t, v in load_fundamental_scores().items()}
    tickers = [
        t for t in all_tickers
        if t in fund_scores and float(fund_scores[t].get("fund_score", 0.0)) >= 6.5
    ]
    print(f"Universo tras gate fundamental >=6.5: {len(tickers)}/{len(all_tickers)}")

    prices = {
        norm_ticker(t): df
        for t, df in download_prices(tickers, years=args.years).items()
    }
    if not prices:
        raise RuntimeError("No hay precios")
    indicators = build_indicators(prices)
    benchmarks = download_context_benchmarks(args.years)
    entries_by_date, signal_counts = build_signals(
        prices, indicators, fund_scores, benchmarks
    )
    price_maps, all_dates = make_price_maps(prices)

    enriched = enrich_signals(
        entries_by_date, prices, indicators, fund_scores, benchmarks
    )
    trades, skipped_overlap = build_episode_dataset(enriched, price_maps)
    if len(trades) < 100:
        raise RuntimeError(f"Muestra insuficiente: {len(trades)} episodios")

    trades, split60, split80 = assign_splits(trades)
    singles, interactions = select_patterns(trades)
    buckets = bucket_diagnostics(trades)
    categories = categorical_diagnostics(trades)

    split_metrics = {
        split: metrics(trades[trades["split"] == split])
        for split in ("train", "validation", "test")
    }
    all_metrics = metrics(trades)
    exit_breakdown = (
        trades.groupby("exit_reason")
        .agg(
            n=("r_multiple_net", "size"),
            win_rate=("win", "mean"),
            avg_r=("r_multiple_net", "mean"),
        )
        .reset_index()
    )
    exit_breakdown["win_rate"] *= 100.0

    robust_singles = [x for x in singles if x["robust_single"]]
    robust_interactions = [x for x in interactions if x["robust_interaction"]]

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    trades.to_csv(out / "pattern_trades.csv", index=False)
    buckets.to_csv(out / "pattern_feature_buckets.csv", index=False)
    categories.to_csv(out / "pattern_categories.csv", index=False)

    flat_candidates = []
    for row in singles:
        flat_candidates.append({
            "type": "single",
            "label": row["label"],
            "robust": row["robust_single"],
            "train_delta_avg_r": row["train_delta_avg_r"],
            "val_delta_avg_r": row.get("val_delta_avg_r"),
            "test_delta_avg_r": row.get("test_delta_avg_r"),
            "train_delta_pf": row["train_delta_pf"],
            "val_delta_pf": row.get("val_delta_pf"),
            "test_delta_pf": row.get("test_delta_pf"),
            "train_keep_rate": row["train_keep_rate"],
            "val_keep_rate": row.get("val_keep_rate"),
            "test_keep_rate": row.get("test_keep_rate"),
            "oos_bootstrap_prob": row.get("oos_bootstrap_prob_kept_gt_removed"),
        })
    for row in interactions:
        flat_candidates.append({
            "type": "interaction",
            "label": row["label"],
            "robust": row["robust_interaction"],
            "train_delta_avg_r": row["train"]["delta_avg_r"],
            "val_delta_avg_r": row["validation"]["delta_avg_r"],
            "test_delta_avg_r": row["test"]["delta_avg_r"],
            "train_delta_pf": row["train"]["delta_pf"],
            "val_delta_pf": row["validation"]["delta_pf"],
            "test_delta_pf": row["test"]["delta_pf"],
            "train_keep_rate": row["train"]["keep_rate"],
            "val_keep_rate": row["validation"]["keep_rate"],
            "test_keep_rate": row["test"]["keep_rate"],
            "oos_bootstrap_prob": np.nan,
        })
    pd.DataFrame(flat_candidates).to_csv(out / "pattern_candidates.csv", index=False)

    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "strategy_version": STRATEGY_VERSION,
        "model_version": MODEL_VERSION,
        "period": {
            "start": all_dates[0] if all_dates else None,
            "end": all_dates[-1] if all_dates else None,
            "years_requested": args.years,
            "split_validation_start": split60,
            "split_test_start": split80,
        },
        "methodology": {
            "episode_rule": "FIRST_FULL_PER_TICKER_UNTIL_PREVIOUS_EPISODE_CLOSES",
            "portfolio_limit": "NONE_FOR_PATTERN_DISCOVERY",
            "entry_proxy": "NEXT_SESSION_OPEN",
            "exit_rules": "TP1_1ATR_50PCT__TP2_1_5ATR__BE_AFTER_TP1__SL5__T7__GAP_AT_OPEN__SL_FIRST",
            "cost_rt_pct": COST_PCT_RT * 100.0,
            "discovery_split": "60pct_chronological",
            "validation_split": "20pct_chronological",
            "final_test_split": "20pct_chronological",
            "fund_score_warning": "Current fund_score is retroactively applied; excluded from threshold discovery.",
            "post_entry_features_excluded_from_discovery": ["exit_reason", "sessions", "tp1_hit", "mfe_r", "mae_r"],
        },
        "signal_counts": signal_counts,
        "enriched_signals": len(enriched),
        "episode_trades": len(trades),
        "same_ticker_overlap_signals_skipped": skipped_overlap,
        "all_metrics": all_metrics,
        "split_metrics": split_metrics,
        "exit_breakdown": exit_breakdown.to_dict("records"),
        "robust_single_patterns": robust_singles[:10],
        "robust_interactions": robust_interactions[:10],
        "top_all_single_candidates": singles[:20],
        "top_all_interactions": interactions[:20],
    }
    with open(out / "pattern_report.json", "w", encoding="utf-8") as fh:
        json.dump(json_clean(report), fh, ensure_ascii=False, indent=2)

    print("\nMUESTRA")
    print(f"  FULL enriquecidas: {len(enriched)}")
    print(f"  Episodios independientes: {len(trades)}")
    print(f"  FULL solapadas mismo ticker descartadas: {skipped_overlap}")
    print(f"  Split: train<{split60} · validation<{split80} · test resto")
    print("\nBASELINE")
    print(json.dumps(json_clean(all_metrics), ensure_ascii=False))
    print("\nROBUST SINGLE PATTERNS")
    if robust_singles:
        for row in robust_singles[:10]:
            print(
                f"  {row['label']} | "
                f"dR train={row['train_delta_avg_r']:+.3f} "
                f"val={row['val_delta_avg_r']:+.3f} "
                f"test={row['test_delta_avg_r']:+.3f} | "
                f"keep test={row['test_keep_rate']:.1%} | "
                f"bootstrap={row.get('oos_bootstrap_prob_kept_gt_removed', np.nan):.3f}"
            )
    else:
        print("  Ninguno superó los criterios estrictos.")
    print("\nROBUST INTERACTIONS")
    if robust_interactions:
        for row in robust_interactions[:10]:
            print(
                f"  {row['label']} | "
                f"dR train={row['train']['delta_avg_r']:+.3f} "
                f"val={row['validation']['delta_avg_r']:+.3f} "
                f"test={row['test']['delta_avg_r']:+.3f}"
            )
    else:
        print("  Ninguna interacción superó los criterios estrictos.")
    print(f"\nResultados: {out}")


if __name__ == "__main__":
    main()
