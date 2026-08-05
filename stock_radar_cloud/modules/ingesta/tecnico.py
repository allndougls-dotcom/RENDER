"""Calcula todos los indicadores técnicos sobre las series OHLCV."""

import numpy as np
import pandas as pd
from tqdm import tqdm
from datetime import datetime


# ── Indicadores base ──────────────────────────────────────────────────────────

def _rsi(c: pd.Series, p: int = 14) -> pd.Series:
    d = c.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return (100 - 100 / (1 + g / l.replace(0, np.nan))).round(2)


def _macd(c: pd.Series, f=12, s=26, sg=9):
    ml = c.ewm(span=f, adjust=False).mean() - c.ewm(span=s, adjust=False).mean()
    sl = ml.ewm(span=sg, adjust=False).mean()
    return ml, sl, ml - sl


def _bb(c: pd.Series, p=20, k=2):
    mid = c.rolling(p).mean()
    std = c.rolling(p).std()
    lo  = mid - k * std
    hi  = mid + k * std
    pct = ((c - lo) / (hi - lo).replace(0, np.nan)).round(4)
    return hi, mid, lo, pct


def _atr(h: pd.Series, l: pd.Series, c: pd.Series, p=14) -> pd.Series:
    tr = pd.concat([
        h - l,
        (h - c.shift()).abs(),
        (l - c.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(p).mean()


def _find_sr(df: pd.DataFrame, lookback=120, pw=5):
    """Detecta soportes y resistencias por swing pivots."""
    rec = df.tail(lookback).reset_index(drop=True)
    lev = []
    for i in range(pw, len(rec) - pw):
        win = rec.iloc[i - pw: i + pw + 1]
        if rec["Low"].iloc[i]  == win["Low"].min():  lev.append({"p": rec["Low"].iloc[i],  "t": "S"})
        if rec["High"].iloc[i] == win["High"].max(): lev.append({"p": rec["High"].iloc[i], "t": "R"})
    if not lev:
        return np.nan, np.nan, 0, 0

    df_l = pd.DataFrame(lev).sort_values("p").reset_index(drop=True)
    g = 0
    df_l["g"] = 0
    for i in range(1, len(df_l)):
        if abs(df_l.loc[i, "p"] - df_l.loc[i-1, "p"]) / df_l.loc[i-1, "p"] >= 0.015:
            g += 1
        df_l.loc[i, "g"] = g

    grp = df_l.groupby("g").agg(price=("p", "mean"), strength=("p", "count"), t=("t", "first")).reset_index(drop=True)
    cp  = rec["Close"].iloc[-1]

    sup = grp[(grp["t"] == "S") & (grp["price"] < cp * 1.02)].sort_values("price", ascending=False)
    res = grp[(grp["t"] == "R") & (grp["price"] > cp * 0.98)].sort_values("price")

    s1p = sup.iloc[0]["price"]    if len(sup) > 0 else np.nan
    s1s = int(sup.iloc[0]["strength"]) if len(sup) > 0 else 0
    r1p = res.iloc[0]["price"]    if len(res) > 0 else np.nan
    r1s = int(res.iloc[0]["strength"]) if len(res) > 0 else 0
    return s1p, r1p, s1s, r1s


def _candle_patterns(df: pd.DataFrame, n=3) -> str:
    rec = df.tail(n + 1)
    pat = []
    for i in range(1, len(rec)):
        c, pv = rec.iloc[i], rec.iloc[i - 1]
        body  = abs(c["Close"] - c["Open"])
        rng   = c["High"] - c["Low"]
        if rng == 0:
            continue
        ls = min(c["Open"], c["Close"]) - c["Low"]
        if body < rng * 0.3 and ls > body * 2:
            pat.append("HAMMER")
        if (c["Close"] > pv["Open"] and c["Open"] < pv["Close"]
                and pv["Close"] < pv["Open"] and c["Close"] > c["Open"]):
            pat.append("BULL_ENGULF")
        if body < rng * 0.1:
            pat.append("DOJI")
    return "|".join(pat) if pat else "NONE"


# ── Motor principal ───────────────────────────────────────────────────────────

def calcular_tecnicos(all_prices: dict) -> pd.DataFrame:
    rows   = []
    errors = []

    for ticker, df in tqdm(all_prices.items(), desc="  Calculando técnicos", unit="ticker"):
        try:
            c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]
            n = len(df)

            # RSI
            rsi_s = _rsi(c)
            rsi_n = rsi_s.iloc[-1]
            rsi_p = rsi_s.iloc[-6] if n > 6 else np.nan

            # MACD
            _, _, hist = _macd(c)
            hist_n = hist.iloc[-1]
            hist_p = hist.iloc[-4] if n > 4 else np.nan

            # Bollinger
            _, _, _, bbp = _bb(c)
            bb_n = bbp.iloc[-1]

            # ATR
            atr_s  = _atr(h, l, c)
            atr_n  = atr_s.iloc[-1]
            atr_60 = atr_s.iloc[-60:].mean() if n >= 60 else atr_s.mean()

            # Medias móviles
            ma50  = c.rolling(50).mean().iloc[-1]  if n >= 50  else np.nan
            ma200 = c.rolling(200).mean().iloc[-1] if n >= 200 else np.nan
            ema20 = c.ewm(span=20, adjust=False).mean().iloc[-1]
            price = c.iloc[-1]

            gc   = bool(ma50 > ma200) if not (np.isnan(ma50) or np.isnan(ma200)) else False
            p200 = (price / ma200 - 1) * 100 if not np.isnan(ma200) else np.nan
            p50  = (price / ma50  - 1) * 100 if not np.isnan(ma50)  else np.nan

            ma200_prev = c.rolling(200).mean().iloc[-21] if n >= 221 else np.nan
            ma200_sl   = (ma200 - ma200_prev) / ma200_prev * 100 if not np.isnan(ma200_prev) else np.nan

            # Drawdown
            high60 = h.iloc[-60:].max() if n >= 60 else h.max()
            dd60   = (price - high60) / high60 * 100
            h52    = h.iloc[-252:].max() if n >= 252 else h.max()
            l52    = l.iloc[-252:].min()  if n >= 252 else l.min()

            # Volumen
            va20 = v.iloc[-20:].mean() if n >= 20 else v.mean()
            vr   = v.iloc[-1] / va20 if va20 > 0 else 1.0
            vdec = bool(v.iloc[-5:].mean() < v.iloc[-60:-5].mean()) if n >= 65 else False

            # Soporte / Resistencia
            s1p, r1p, s1s, r1s = _find_sr(df)
            near_s = bool(not np.isnan(s1p) and abs(s1p - price) / price < 0.04) if not np.isnan(s1p) else False

            # Patrón de velas
            cpat = _candle_patterns(df)

            # Tendencia
            if not np.isnan(ma200):
                tbias = "ALCISTA" if (price > ma50 and price > ma200) else ("NEUTRO" if price > ma200 else "BAJISTA")
            else:
                tbias = "NEUTRO"

            # Score técnico
            ts   = min(5 + (1.5 if price > ma50 else 0) + (1.5 if price > ma200 else 0)
                       + (1.5 if gc else 0) + (0.5 if not np.isnan(ma200_sl) and ma200_sl > 0 else 0), 10)
            ms   = min(5 + (2.5 if rsi_n < 30 else 1.5 if rsi_n < 40 else 0)
                       + (1 if rsi_n > rsi_p else 0) + (1 if hist_n > hist_p else 0), 10)
            vs   = min(5 + (3 if bb_n < 0.15 else 1.5 if bb_n < 0.3 else 0)
                       + (2 if atr_n / atr_60 < 0.8 else 0), 10)
            vols = min(5 + (2.5 if vdec else 0) + (2 if vr > 1.3 and price > c.iloc[-2] else 0), 10)
            ss   = min(4 + (3 if near_s else 0) + (2 if s1s >= 3 else 0)
                       + (1 if not np.isnan(ma200) and ma200 * 0.92 < price < ma200 else 0), 10)
            cs   = min(5 + (2 if "HAMMER" in cpat else 0) + (2.5 if "BULL_ENGULF" in cpat else 0), 10)

            tech_score = round(ts * .20 + ms * .20 + vs * .15 + vols * .20 + ss * .15 + cs * .10, 2)
            sl_pct     = atr_n * 2 / price * 100
            tg_pct     = atr_n * 3 / price * 100

            rows.append({
                "ticker":              ticker,
                "price":               round(price, 2),
                "data_vintage":        datetime.today().strftime("%Y-%m-%d"),
                "rsi_14":              round(rsi_n, 2),
                "rsi_5d_ago":          round(rsi_p, 2) if not np.isnan(rsi_p) else np.nan,
                "rsi_trend":           "UP" if rsi_n > rsi_p else "DOWN",
                "macd_hist":           round(hist_n, 4),
                "macd_hist_3d_ago":    round(hist_p, 4) if not np.isnan(hist_p) else np.nan,
                "macd_improving":      bool(hist_n > hist_p),
                "bb_pct_b":            round(bb_n, 4),
                "atr_14":              round(atr_n, 4),
                "atr_ratio_60d":       round(atr_n / atr_60, 3),
                "atr_pct_price":       round(atr_n / price * 100, 3),
                "sma_50":              round(ma50,  2) if not np.isnan(ma50)  else np.nan,
                "sma_200":             round(ma200, 2) if not np.isnan(ma200) else np.nan,
                "ema_20":              round(ema20, 2),
                "price_vs_50ma_pct":   round(p50,  2) if not np.isnan(p50)  else np.nan,
                "price_vs_200ma_pct":  round(p200, 2) if not np.isnan(p200) else np.nan,
                "golden_cross":        gc,
                "ma200_slope_20d":     round(ma200_sl, 4) if not np.isnan(ma200_sl) else np.nan,
                "trend_bias":          tbias,
                "drawdown_60d":        round(dd60, 2),
                "high_60d":            round(high60, 2),
                "high_52w":            round(h52, 2),
                "low_52w":             round(l52, 2),
                "pct_from_52w_high":   round((price / h52 - 1) * 100, 2),
                "pct_from_52w_low":    round((price / l52 - 1) * 100, 2),
                "volume_ratio_20d":    round(vr, 3),
                "volume_decreasing":   vdec,
                "support_1_price":     round(s1p, 2) if not np.isnan(s1p) else np.nan,
                "support_1_strength":  s1s,
                "resistance_1_price":  round(r1p, 2) if not np.isnan(r1p) else np.nan,
                "near_support":        near_s,
                "dist_support_pct":    round((s1p - price) / price * 100, 2) if not np.isnan(s1p) else np.nan,
                "candle_pattern":      cpat,
                "tech_score":          tech_score,
                "tech_trend":          round(ts, 2),
                "tech_momentum":       round(ms, 2),
                "tech_volume":         round(vols, 2),
                "tech_support":        round(ss, 2),
                "tech_volatility":     round(vs, 2),
                "tech_candle":         round(cs, 2),
                "setup_hot":           bool(rsi_n < 40 and dd60 <= -8 and hist_n > hist_p and vdec),
                "stop_loss_atr_pct":   round(sl_pct, 2),
                "target_atr_pct":      round(tg_pct, 2),
                "risk_reward":         round(tg_pct / sl_pct, 2) if sl_pct > 0 else np.nan,
            })

        except Exception as e:
            errors.append(ticker)

    df = pd.DataFrame(rows)
    hot = df["setup_hot"].sum() if len(df) > 0 else 0
    print(f"  ✅ Técnico: {len(df)} empresas · Errores: {len(errors)} · HOT: {hot}")
    return df
