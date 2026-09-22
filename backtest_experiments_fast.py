"""Fast launcher for backtest_experiments.py.

Caches the expensive price/date indexes and T+1 schedules across experiments.
It does not change any trading rule or statistic; it only avoids rebuilding
identical structures for every variant.
"""

import backtest_experiments as exp

_original_price_indexes = exp.price_indexes
_original_schedule_entries = exp.schedule_entries
_cached_rows = None
_schedule_cache = {}


def cached_price_indexes(prices):
    global _cached_rows
    if _cached_rows is None:
        _cached_rows = _original_price_indexes(prices)
    return _cached_rows


def cached_schedule_entries(signal_map, indicators):
    key = id(signal_map)
    if key not in _schedule_cache:
        _schedule_cache[key] = _original_schedule_entries(signal_map, indicators)
    return _schedule_cache[key]


exp.price_indexes = cached_price_indexes
exp.schedule_entries = cached_schedule_entries

if __name__ == "__main__":
    exp.main()
