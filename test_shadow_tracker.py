import sys

sys.path.insert(0, "modules")

from shadow_tracker import _replay


def signal(ticker, score=8.0, atr=4.0):
    return {
        "ticker": ticker,
        "signal_date": "2026-01-01",
        "strategy_version": "SIDI_SHADOW_V1",
        "company": ticker,
        "sector": "Test",
        "combined_score": score,
        "fund_score": 7.0,
        "dd60": 12.0,
        "rsi14": 30.0,
        "atr14_signal": atr,
        "signal_close": 99.0,
        "snapshot_json": "{}",
    }


def test_sl_first_when_tp_and_sl_touch_same_session():
    prices = {"AAA": {"2026-01-02": {"open": 100, "high": 104, "low": 94, "close": 101}}}
    result = _replay([signal("AAA")], prices)[0]
    assert result["exit_reason"] == "SL"
    assert result["exit_price"] == 95.0


def test_gap_below_stop_exits_at_real_open():
    prices = {"AAA": {
        "2026-01-02": {"open": 100, "high": 101, "low": 99, "close": 100},
        "2026-01-05": {"open": 94, "high": 96, "low": 93, "close": 95},
    }}
    result = _replay([signal("AAA")], prices)[0]
    assert result["exit_reason"] == "GAP_SL"
    assert result["exit_price"] == 94


def test_only_five_positions_open():
    tickers = list("ABCDEF")
    prices = {t: {"2026-01-02": {"open": 100, "high": 100.5, "low": 99, "close": 100}} for t in tickers}
    results = _replay([signal(t, 9 - i * 0.1, 1) for i, t in enumerate(tickers)], prices)
    assert sum(x["status"] == "OPEN" for x in results) == 5
    assert sum(x["status"] == "SKIPPED_NO_SLOT" for x in results) == 1


def test_time_stop_closes_on_seventh_session():
    bars = {
        f"2026-01-{day:02d}": {"open": 100, "high": 101, "low": 99, "close": 100 + day / 100}
        for day in [2, 5, 6, 7, 8, 9, 12]
    }
    result = _replay([signal("AAA", atr=10)], {"AAA": bars})[0]
    assert result["exit_reason"] == "T7"
    assert result["sessions_held"] == 7
