"""Fast launcher + second-round combinations for backtest_experiments.py.

Caches expensive structures across experiments and appends combinations of the
best first-round ideas. Trading logic remains in backtest_experiments.py.
"""

import backtest_experiments as exp

_original_price_indexes = exp.price_indexes
_original_schedule_entries = exp.schedule_entries
_original_build_experiments = exp.build_experiments
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


def experiments_with_combinations():
    exps = list(_original_build_experiments())
    E = exp.Experiment
    exps.extend([
        E("ATR075_RANK", target_mode="atr", atr_mult=0.75, rank_signals=True,
          description="0.75xATR target + quality ranking"),
        E("ATR075_T7", target_mode="atr", atr_mult=0.75, time_stop=7,
          description="0.75xATR target + 7-day time stop"),
        E("ATR075_RANK_T7", target_mode="atr", atr_mult=0.75, time_stop=7, rank_signals=True,
          description="0.75xATR target + ranking + 7-day time stop"),
        E("ATR075_T5", target_mode="atr", atr_mult=0.75, time_stop=5,
          description="0.75xATR target + 5-day time stop"),
        E("ATR075_STOPATR15", target_mode="atr", atr_mult=0.75,
          stop_mode="atr", stop_atr_mult=1.5,
          description="0.75xATR target + 1.5xATR stop"),
        E("ATR075_STOPATR2", target_mode="atr", atr_mult=0.75,
          stop_mode="atr", stop_atr_mult=2.0,
          description="0.75xATR target + 2xATR stop"),
        E("TP3_RANK_T7", target_mode="fixed", target_pct=0.03, time_stop=7, rank_signals=True,
          description="3% fixed target + ranking + 7-day time stop"),
        E("TP4_RANK", target_mode="fixed", target_pct=0.04, rank_signals=True,
          description="4% fixed target + quality ranking"),
        E("TP4_RANK_T7", target_mode="fixed", target_pct=0.04, time_stop=7, rank_signals=True,
          description="4% fixed target + ranking + 7-day time stop"),
        E("TP65_RANK_T7", target_mode="fixed", target_pct=0.065, time_stop=7, rank_signals=True,
          description="6.5% fixed target + ranking + 7-day time stop"),
        E("TP8_RANK", target_mode="fixed", target_pct=0.08, rank_signals=True,
          description="8% fixed target + quality ranking"),
        E("TP8_RANK_T7", target_mode="fixed", target_pct=0.08, time_stop=7, rank_signals=True,
          description="8% fixed target + ranking + 7-day time stop"),
        E("ATR15_RANK_T7", target_mode="atr", atr_mult=1.5, time_stop=7, rank_signals=True,
          description="1.5xATR target + ranking + 7-day time stop"),
    ])
    return exps


exp.price_indexes = cached_price_indexes
exp.schedule_entries = cached_schedule_entries
exp.build_experiments = experiments_with_combinations

if __name__ == "__main__":
    exp.main()
