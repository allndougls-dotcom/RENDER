import argparse
import os
import re
import time

import ccxt
import pandas as pd


def date_to_ms(value: str, end: bool = False) -> int:
    only_date = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    if end and only_date:
        ts = ts + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
    return int(ts.timestamp() * 1000)


def safe_filename(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_").replace("-", "_")


def download_symbol(exchange, symbol, timeframe, start_ms, end_ms, limit=1000):
    timeframe_ms = exchange.parse_timeframe(timeframe) * 1000
    since = start_ms
    rows = []
    page = 0

    while since <= end_ms:
        for attempt in range(5):
            try:
                batch = exchange.fetch_ohlcv(
                    symbol,
                    timeframe=timeframe,
                    since=since,
                    limit=limit,
                )
                break
            except ccxt.NetworkError as exc:
                if attempt == 4:
                    raise
                sleep_seconds = 2 ** attempt
                print(f"Error de red: {exc}. Reintento en {sleep_seconds}s...")
                time.sleep(sleep_seconds)

        if not batch:
            break

        page += 1
        last_raw_timestamp = batch[-1][0]
        rows.extend(row for row in batch if start_ms <= row[0] <= end_ms)

        if page % 20 == 0:
            last_date = pd.to_datetime(last_raw_timestamp, unit="ms", utc=True)
            print(f"{symbol}: {len(rows):,} velas hasta {last_date}")

        if last_raw_timestamp >= end_ms:
            break

        new_since = last_raw_timestamp + timeframe_ms
        if new_since <= since:
            raise RuntimeError("El exchange no está avanzando en la paginación.")
        since = new_since

    if not rows:
        raise RuntimeError(f"No se han obtenido datos para {symbol}")

    df = pd.DataFrame(
        rows,
        columns=["timestamp_ms", "open", "high", "low", "close", "volume"],
    )
    df = df.drop_duplicates("timestamp_ms").sort_values("timestamp_ms").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df["exchange"] = exchange.id
    df["symbol"] = symbol
    df["timeframe"] = timeframe

    return df[
        [
            "timestamp",
            "timestamp_ms",
            "exchange",
            "symbol",
            "timeframe",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--symbols", required=True, help='Ejemplo: "BTC/USDT,ETH/USDT"')
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--format", choices=["parquet", "csv"], default="parquet")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()

    if not hasattr(ccxt, args.exchange):
        raise ValueError(f"Exchange desconocido en CCXT: {args.exchange}")

    exchange = getattr(ccxt, args.exchange)({"enableRateLimit": True, "timeout": 30000})
    exchange.load_markets()

    if not exchange.has.get("fetchOHLCV"):
        raise RuntimeError(f"{args.exchange} no soporta fetchOHLCV")
    if args.timeframe not in exchange.timeframes:
        raise ValueError(f"Timeframe {args.timeframe} no soportado")

    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()]
    start_ms = date_to_ms(args.start)
    end_ms = date_to_ms(args.end, end=True)
    os.makedirs(args.output_dir, exist_ok=True)

    for symbol in symbols:
        if symbol not in exchange.markets:
            print(f"AVISO: {symbol} no existe en {args.exchange}")
            continue

        print(f"\nDescargando {symbol}...")
        df = download_symbol(exchange, symbol, args.timeframe, start_ms, end_ms, args.limit)
        filename = (
            f"{safe_filename(symbol)}_{args.timeframe}_"
            f"{args.start.replace('-', '')}_{args.end.replace('-', '')}"
        )

        if args.format == "parquet":
            path = os.path.join(args.output_dir, filename + ".parquet")
            df.to_parquet(path, index=False, compression="zstd")
        else:
            path = os.path.join(args.output_dir, filename + ".csv")
            df.to_csv(path, index=False)

        print(f"Guardado: {path}")
        print(f"Velas: {len(df):,}")


if __name__ == "__main__":
    main()
