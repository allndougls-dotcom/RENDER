# SIDI — Experimental backtest results

Date: 2026-09-22

## Scope

This branch intentionally does **not** replace the production `backtest.py`. It adds a stricter experimental simulator and GitHub Actions workflow so changes can be evaluated before merging.

Experimental execution assumptions:

- Signal is generated at the close of day T.
- Entry is at the **Open of the next trading day (T+1)**.
- TP and SL are active from the entry session itself.
- If the daily OHLC candle touches both TP and SL, the result is counted as **STOP** (conservative ambiguity rule).
- Risk per position: **1.5%** of realised capital.
- Maximum simultaneous positions: **5**.
- Max drawdown uses a **daily mark-to-market equity curve**.
- Test period: **2024-08-11 to 2026-09-10**.
- Price universe: 501/503 tickers available (FDXF and HONA unavailable in the run).

Base signal: RSI < 40, DD60 >= 12%, MACD histogram improving, declining volume condition, current `fund_score >= 6.5` proxy.

## Main limitation

The fundamental score is still today's/current score applied retrospectively. It is **not point-in-time fundamental data**, so results involving `fund_score` remain provisional and can contain look-ahead/survivorship-style bias. Transaction costs/slippage are also not yet included.

## Main results

| Variant | WR | PF | Return | CAGR | MDD | Avg days | Trades |
|---|---:|---:|---:|---:|---:|---:|---:|
| Base: 1.5xATR, SL 5%, 15d | 52.78% | 1.236 | +68.99% | 28.68% | -20.95% | 6.00 | 324 |
| TP fixed 2% | 76.99% | 1.353 | +99.61% | 39.40% | -19.46% | 2.93 | 578 |
| TP fixed 3% | 68.38% | 1.313 | +98.40% | 38.99% | -16.02% | 3.85 | 468 |
| TP fixed 4% | 62.37% | 1.346 | +103.83% | 40.81% | -17.20% | 4.87 | 388 |
| TP fixed 6.5% | 52.29% | 1.377 | +113.20% | 43.88% | -22.63% | 6.45 | 306 |
| TP fixed 8% | 49.26% | 1.462 | +125.72% | 47.88% | -24.96% | 7.31 | 270 |
| ATR 0.75x | 69.57% | 1.344 | +125.95% | 47.96% | -17.20% | 3.54 | 493 |
| ATR 2.5x | 47.69% | 1.433 | +135.50% | 50.93% | -32.19% | 7.75 | 260 |
| Time stop 7d (base TP) | 54.40% | 1.337 | +119.27% | 45.84% | -18.46% | 4.84 | 375 |
| Quality ranking (base TP) | 53.14% | 1.311 | +102.09% | 40.23% | -20.95% | 6.05 | 318 |
| Confirmation close > prior high | 53.57% | 1.186 | +27.17% | 12.25% | -22.88% | 5.93 | 196 |
| **ATR 0.75x + time stop 7d** | **67.66%** | **1.359** | **+146.71%** | **54.34%** | **-15.57%** | **3.19** | **535** |
| ATR 0.75x + ranking + time stop 7d | 66.91% | 1.330 | +137.96% | 51.68% | -18.40% | 3.13 | 541 |
| TP 8% + ranking + time stop 7d | 52.21% | 1.384 | +131.39% | 49.66% | -17.51% | 5.10 | 362 |

## Current experimental candidate

The strongest tested risk/return combination on this sample is:

- DD60 >= 12%
- fund_score >= 6.5 (current-score proxy; must be replaced with point-in-time data)
- RSI < 40 plus existing MACD/volume signal
- Entry next trading day at Open
- Fixed 5% stop
- **TP = entry + 0.75 x ATR(14)**
- **Time stop = 7 trading sessions**
- Risk = 1.5% of capital
- Max 5 positions

Result in this sample: **WR 67.66%, PF 1.359, return +146.71%, CAGR 54.34%, MDD -15.57%, 535 trades, average holding 3.19 sessions**.

The average realised trade was approximately **+0.596%** (~EUR 27.42 in this simulation), so spread/slippage/FX sensitivity is essential before production use.

## What did not improve the strategy

- Requiring `close > previous high` confirmation dramatically reduced opportunity count and return.
- Raising minimum DD from 12% to 15/18/20% progressively reduced performance in this sample; DD12 remains the best tested threshold around that region.
- ATR-based stops increased WR when widened, but materially reduced total return. The fixed 5% stop remained stronger for capital growth in these tests.
- Ranking improved the original 1.5xATR base but did not improve the `0.75xATR + 7d` candidate, so ranking should not be added automatically.

## Next validation steps before production

1. Add realistic spread/slippage/FX/commission assumptions (especially important for the 0.75xATR target).
2. Replace current fundamental scores with **point-in-time** historical fundamentals.
3. Run walk-forward/out-of-sample windows and market-regime stress tests.
4. Test TP1/TP2 logic (e.g. 50% at 0.75xATR, move residual stop to break-even, then TP2 at 1.5-2xATR).
5. Test earnings blackout windows (3/5/7 sessions).
6. Test relative drawdown versus sector/SPY without data leakage.

No production rule should be changed solely from this in-sample experiment until steps 1-3 are passed.
