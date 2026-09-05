import argparse
import itertools
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
from numba import njit


@njit(cache=True, nogil=True)
def search_left(a, x):
    lo, hi = 0, len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if a[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return lo


@njit(cache=True, nogil=True)
def search_right(a, x):
    lo, hi = 0, len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if a[mid] <= x:
            lo = mid + 1
        else:
            hi = mid
    return lo


@njit(cache=True, nogil=True)
def process_segment(p0, p1, levels, states, quantity, quote_balance, base_balance, fee_rate):
    fills = 0
    fees = 0.0
    n_intervals = len(states)

    if p1 > p0:
        start_k = search_right(levels, p0)
        end_k = search_right(levels, p1)
        for k in range(start_k, end_k):
            interval = k - 1
            if 0 <= interval < n_intervals and states[interval] == 1:
                sell_price = levels[k]
                if base_balance + 1e-12 >= quantity:
                    gross = quantity * sell_price
                    fee = gross * fee_rate
                    base_balance -= quantity
                    quote_balance += gross - fee
                    fees += fee
                    fills += 1
                    states[interval] = 0

    elif p1 < p0:
        start_k = search_left(levels, p1)
        end_k = search_left(levels, p0)
        for k in range(end_k - 1, start_k - 1, -1):
            interval = k
            if 0 <= interval < n_intervals and states[interval] == 0:
                buy_price = levels[k]
                gross = quantity * buy_price
                fee = gross * fee_rate
                total_cost = gross + fee
                if quote_balance + 1e-9 >= total_cost:
                    quote_balance -= total_cost
                    base_balance += quantity
                    fees += fee
                    fills += 1
                    states[interval] = 1

    return quote_balance, base_balance, fills, fees


@njit(cache=True, nogil=True)
def simulate_grid(opens, highs, lows, closes, levels, capital, fee_rate):
    n_intervals = len(levels) - 1
    if n_intervals < 2:
        return np.nan, np.nan, 0, np.nan, np.nan

    states = np.zeros(n_intervals, dtype=np.int8)
    start_price = opens[0]
    denominator = 0.0

    for i in range(n_intervals):
        lower = levels[i]
        upper = levels[i + 1]
        if start_price >= upper:
            states[i] = 0
            denominator += lower * (1.0 + fee_rate)
        elif start_price <= lower:
            states[i] = 1
            denominator += start_price
        else:
            midpoint = (lower + upper) / 2.0
            if start_price >= midpoint:
                states[i] = 0
                denominator += lower * (1.0 + fee_rate)
            else:
                states[i] = 1
                denominator += start_price

    if denominator <= 0:
        return np.nan, np.nan, 0, np.nan, np.nan

    quantity = capital / denominator
    quote_balance = 0.0
    base_balance = 0.0

    for i in range(n_intervals):
        if states[i] == 0:
            quote_balance += quantity * levels[i] * (1.0 + fee_rate)
        else:
            base_balance += quantity

    total_fills = 0
    total_fees = 0.0
    initial_equity = quote_balance + base_balance * start_price
    peak_equity = initial_equity
    max_drawdown = 0.0
    previous_close = opens[0]

    for j in range(len(opens)):
        o, h, l, c = opens[j], highs[j], lows[j], closes[j]

        if j > 0:
            quote_balance, base_balance, fills, fees = process_segment(
                previous_close, o, levels, states, quantity,
                quote_balance, base_balance, fee_rate
            )
            total_fills += fills
            total_fees += fees
            equity = quote_balance + base_balance * o
            if equity > peak_equity:
                peak_equity = equity
            if peak_equity > 0:
                dd = (peak_equity - equity) / peak_equity
                if dd > max_drawdown:
                    max_drawdown = dd

        if c >= o:
            segment_starts = (o, l, h)
            segment_ends = (l, h, c)
        else:
            segment_starts = (o, h, l)
            segment_ends = (h, l, c)

        for s in range(3):
            p0, p1 = segment_starts[s], segment_ends[s]
            quote_balance, base_balance, fills, fees = process_segment(
                p0, p1, levels, states, quantity,
                quote_balance, base_balance, fee_rate
            )
            total_fills += fills
            total_fees += fees
            equity = quote_balance + base_balance * p1
            if equity > peak_equity:
                peak_equity = equity
            if peak_equity > 0:
                dd = (peak_equity - equity) / peak_equity
                if dd > max_drawdown:
                    max_drawdown = dd

        previous_close = c

    final_equity = quote_balance + base_balance * closes[-1]
    return final_equity, max_drawdown * 100.0, total_fills, total_fees, quantity


def load_data(path):
    if path.lower().endswith(".parquet"):
        df = pd.read_parquet(path)
    elif path.lower().endswith(".csv"):
        df = pd.read_csv(path)
    else:
        raise ValueError("El archivo debe ser .parquet o .csv")

    required = ["timestamp", "open", "high", "low", "close"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"Faltan columnas: {missing}")

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return (
        df.dropna(subset=["open", "high", "low", "close"])
        .drop_duplicates("timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def make_levels(anchor_price, lower_pct, upper_pct, n_grids, grid_type):
    lower_price = anchor_price * (1.0 - lower_pct / 100.0)
    upper_price = anchor_price * (1.0 + upper_pct / 100.0)
    if lower_price <= 0:
        raise ValueError("El rango inferior produce precio <= 0")
    if grid_type == "arithmetic":
        return np.linspace(lower_price, upper_price, n_grids + 1, dtype=np.float64)
    if grid_type == "geometric":
        return np.geomspace(lower_price, upper_price, n_grids + 1).astype(np.float64)
    raise ValueError(f"Tipo desconocido: {grid_type}")


def dataframe_to_arrays(df):
    return tuple(
        np.ascontiguousarray(df[col].values, dtype=np.float64)
        for col in ["open", "high", "low", "close"]
    )


def evaluate_config(config, arrays, timestamps, capital, fee_rate):
    lower_pct, upper_pct, n_grids, grid_type = config
    opens, highs, lows, closes = arrays
    anchor = opens[0]
    levels = make_levels(anchor, lower_pct, upper_pct, n_grids, grid_type)
    final_equity, max_drawdown, fills, fees, quantity = simulate_grid(
        opens, highs, lows, closes, levels, capital, fee_rate
    )

    return_pct = (final_equity / capital - 1.0) * 100.0
    bh_quantity = capital / (anchor * (1.0 + fee_rate))
    bh_final = bh_quantity * closes[-1]
    bh_return_pct = (bh_final / capital - 1.0) * 100.0
    excess_vs_hold = return_pct - bh_return_pct

    seconds = (timestamps.iloc[-1] - timestamps.iloc[0]).total_seconds()
    days = max(seconds / 86400.0, 1e-9)
    years = days / 365.25
    cagr = ((final_equity / capital) ** (1.0 / years) - 1.0) * 100.0 if years > 0 and final_equity > 0 else np.nan
    calmar = cagr / max_drawdown if max_drawdown > 0 else np.nan
    return_over_dd = return_pct / max_drawdown if max_drawdown > 0 else np.nan
    step_pcts = np.diff(levels) / levels[:-1] * 100.0

    return {
        "lower_pct": lower_pct,
        "upper_pct": upper_pct,
        "grids": n_grids,
        "grid_type": grid_type,
        "lower_price": levels[0],
        "upper_price": levels[-1],
        "min_grid_step_pct": step_pcts.min(),
        "max_grid_step_pct": step_pcts.max(),
        "order_base_qty": quantity,
        "final_equity": final_equity,
        "return_pct": return_pct,
        "cagr_pct": cagr,
        "max_drawdown_pct": max_drawdown,
        "calmar": calmar,
        "return_over_dd": return_over_dd,
        "fills": fills,
        "fills_per_day": fills / days,
        "fees_paid": fees,
        "fee_drag_pct": fees / capital * 100.0,
        "buy_hold_return_pct": bh_return_pct,
        "excess_vs_hold_pct": excess_vs_hold,
    }


def parse_float_list(value):
    return [float(x) for x in value.split(",")]


def parse_int_list(value):
    return [int(x) for x in value.split(",")]


def run_search(df, configs, capital, fee_rate, workers):
    arrays = dataframe_to_arrays(df)
    timestamps = df["timestamp"]
    results = []

    evaluate_config(configs[0], arrays, timestamps, capital, fee_rate)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(evaluate_config, config, arrays, timestamps, capital, fee_rate): config
            for config in configs
        }
        completed = 0
        for future in as_completed(futures):
            results.append(future.result())
            completed += 1
            if completed % 25 == 0:
                print(f"Procesadas {completed}/{len(configs)} configuraciones")

    return pd.DataFrame(results)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--capital", type=float, default=10000.0)
    parser.add_argument("--fee", type=float, default=0.001)
    parser.add_argument("--lower-pcts", default="5,10,15,20,30")
    parser.add_argument("--upper-pcts", default="5,10,15,20,30")
    parser.add_argument("--grids", default="10,20,30,40,60")
    parser.add_argument("--grid-types", default="arithmetic,geometric")
    parser.add_argument("--train-ratio", type=float, default=0.70)
    parser.add_argument(
        "--objective",
        default="calmar",
        choices=["calmar", "return_pct", "cagr_pct", "return_over_dd", "excess_vs_hold_pct"],
    )
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--output-prefix", default="grid_results")
    args = parser.parse_args()

    if not 0.50 <= args.train_ratio <= 0.95:
        raise ValueError("train-ratio debe estar entre 0.50 y 0.95")

    df = load_data(args.input)
    if len(df) < 100:
        raise ValueError("El dataset es demasiado pequeño")

    split_idx = int(len(df) * args.train_ratio)
    train = df.iloc[:split_idx].copy().reset_index(drop=True)
    test = df.iloc[split_idx:].copy().reset_index(drop=True)

    configs = list(
        itertools.product(
            parse_float_list(args.lower_pcts),
            parse_float_list(args.upper_pcts),
            parse_int_list(args.grids),
            [x.strip() for x in args.grid_types.split(",")],
        )
    )

    print(f"Velas: {len(df):,} | TRAIN: {len(train):,} | TEST: {len(test):,}")
    print(f"Configuraciones: {len(configs):,}")

    train_results = run_search(train, configs, args.capital, args.fee, args.workers)
    train_results = train_results.sort_values(args.objective, ascending=False).reset_index(drop=True)
    train_path = args.output_prefix + "_train_all.csv"
    train_results.to_csv(train_path, index=False)

    top_train = train_results.head(args.top)
    test_arrays = dataframe_to_arrays(test)
    test_timestamps = test["timestamp"]
    test_rows = []

    for _, row in top_train.iterrows():
        config = (float(row["lower_pct"]), float(row["upper_pct"]), int(row["grids"]), row["grid_type"])
        test_rows.append(evaluate_config(config, test_arrays, test_timestamps, args.capital, args.fee))

    test_results = pd.DataFrame(test_rows)
    config_cols = ["lower_pct", "upper_pct", "grids", "grid_type"]
    metric_cols = [col for col in train_results.columns if col not in config_cols]
    train_top_prefixed = top_train[config_cols + metric_cols].rename(columns={col: f"train_{col}" for col in metric_cols})
    test_prefixed = test_results[config_cols + metric_cols].rename(columns={col: f"test_{col}" for col in metric_cols})
    comparison = pd.merge(train_top_prefixed, test_prefixed, on=config_cols, how="left")
    comparison_path = args.output_prefix + "_top_test.csv"
    comparison.to_csv(comparison_path, index=False)

    best = comparison.iloc[0]
    print("\nGANADOR TRAIN")
    print(f"Rango: -{best['lower_pct']:.1f}% / +{best['upper_pct']:.1f}%")
    print(f"Grids: {int(best['grids'])} | Tipo: {best['grid_type']}")
    print(f"TRAIN: {best['train_return_pct']:.2f}% | DD {best['train_max_drawdown_pct']:.2f}% | Calmar {best['train_calmar']:.3f}")
    print(f"TEST:  {best['test_return_pct']:.2f}% | DD {best['test_max_drawdown_pct']:.2f}% | Calmar {best['test_calmar']:.3f}")
    print(f"Vs HODL TEST: {best['test_excess_vs_hold_pct']:+.2f}%")
    print(f"\nResultados: {train_path} y {comparison_path}")


if __name__ == "__main__":
    main()
