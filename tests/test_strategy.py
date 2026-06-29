"""Tests for strategy.py — pure functions (no Alpaca calls)."""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone

from strategy import (
    FilterResult,
    PremkLevels,
    detect_flat_top_breakout,
    detect_pm_flag_break,
    detect_pm_pivot_break,
    detect_premarket_high_break,
    detect_red_to_green,
    mark_premarket_levels,
    run_setup_detectors,
)


def _make_bars(closes: list[float], highs: list[float] = None, lows: list[float] = None,
               opens: list[float] = None, volumes: list[float] = None) -> pd.DataFrame:
    n = len(closes)
    highs = highs or [c * 1.005 for c in closes]
    lows = lows or [c * 0.995 for c in closes]
    opens = opens or closes[:]
    volumes = volumes or [100_000] * n
    idx = pd.date_range("2024-01-15 09:30", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )


class TestMarkPremarketLevels:
    def test_none_on_empty(self):
        assert mark_premarket_levels(pd.DataFrame()) is None

    def test_none_on_single_bar(self):
        assert mark_premarket_levels(_make_bars([10.0])) is None

    def test_high_low_vwap(self):
        bars = _make_bars([10.0, 11.0, 9.0, 10.5],
                          highs=[10.5, 11.5, 9.5, 11.0],
                          lows=[9.5, 10.5, 8.5, 10.0])
        levels = mark_premarket_levels(bars)
        assert levels is not None
        assert levels.premarket_high == pytest.approx(11.5, abs=0.001)
        assert levels.premarket_low == pytest.approx(8.5, abs=0.001)
        assert levels.vwap > 0

    def test_vwap_weighted_by_volume(self):
        bars = _make_bars(
            closes=[10.0, 20.0],
            highs=[10.0, 20.0],
            lows=[10.0, 20.0],
            volumes=[1, 99],
        )
        levels = mark_premarket_levels(bars)
        # Heavy weight on bar 2 (price 20) → vwap closer to 20
        assert levels.vwap > 15.0

    def test_flag_detection(self):
        # 4 tight bars (< 0.5% range)
        closes = [10.0, 10.01, 10.02, 10.01, 10.02]
        bars = _make_bars(closes,
                          highs=[c + 0.01 for c in closes],
                          lows=[c - 0.01 for c in closes])
        levels = mark_premarket_levels(bars)
        assert levels is not None
        assert len(levels.flags) >= 1

    def test_pivot_detection(self):
        # Bar at index 2 is higher than neighbors → pivot high
        closes = [10.0, 10.5, 11.0, 10.5, 10.0]
        highs =  [10.2, 10.7, 11.2, 10.7, 10.2]
        lows =   [9.8,  10.3, 10.8, 10.3, 9.8]
        bars = _make_bars(closes, highs=highs, lows=lows)
        levels = mark_premarket_levels(bars)
        pivot_highs = [p for p in levels.pivots if p["type"] == "high"]
        assert len(pivot_highs) >= 1


class TestDetectPremarketHighBreak:
    def test_triggered_when_close_above_pm_high(self):
        bars = _make_bars([12.0])
        ok, name, price, stop = detect_premarket_high_break(bars, pm_high=11.5)
        assert ok is True
        assert name == "premarket_high_break"
        assert price == 11.5

    def test_not_triggered_when_close_below_pm_high(self):
        bars = _make_bars([11.0])
        ok, *_ = detect_premarket_high_break(bars, pm_high=11.5)
        assert ok is False

    def test_empty_bars(self):
        ok, *_ = detect_premarket_high_break(pd.DataFrame(), pm_high=11.5)
        assert ok is False


class TestDetectPmFlagBreak:
    def test_triggered_above_flag_top(self):
        flags = [{"top": 10.5, "bottom": 10.0, "start_index": 0, "end_index": 3}]
        bars = _make_bars([10.8])
        ok, name, trigger, stop_ref = detect_pm_flag_break(bars, flags)
        assert ok is True
        assert stop_ref == pytest.approx(10.0)

    def test_not_triggered_below_flag_top(self):
        flags = [{"top": 10.5, "bottom": 10.0, "start_index": 0, "end_index": 3}]
        bars = _make_bars([10.3])
        ok, *_ = detect_pm_flag_break(bars, flags)
        assert ok is False

    def test_no_flags_returns_false(self):
        ok, *_ = detect_pm_flag_break(_make_bars([11.0]), [])
        assert ok is False


class TestDetectPmPivotBreak:
    def test_triggered_above_pivot_high(self):
        pivots = [{"type": "high", "price": 10.5, "bar_index": 2},
                  {"type": "low", "price": 9.5, "bar_index": 3}]
        bars = _make_bars([10.8])
        ok, name, trigger, stop_ref = detect_pm_pivot_break(bars, pivots)
        assert ok is True
        assert trigger == pytest.approx(10.5)
        assert stop_ref == pytest.approx(9.5)

    def test_not_triggered_below_pivot_high(self):
        pivots = [{"type": "high", "price": 10.5, "bar_index": 2}]
        bars = _make_bars([10.0])
        ok, *_ = detect_pm_pivot_break(bars, pivots)
        assert ok is False


class TestDetectRedToGreen:
    def test_triggered_prev_below_close_green(self):
        # prev bar close < prev_close, current bar close >= prev_close
        bars = _make_bars([9.80, 10.05])  # first bar red, second bar above prev_close
        ok, name, trigger, stop_ref = detect_red_to_green(bars, prev_close=10.00)
        assert ok is True
        assert stop_ref == pytest.approx(10.00 * 0.995, abs=0.001)

    def test_not_triggered_when_both_bars_green(self):
        bars = _make_bars([10.10, 10.20])
        ok, *_ = detect_red_to_green(bars, prev_close=10.00)
        assert ok is False

    def test_needs_at_least_two_bars(self):
        ok, *_ = detect_red_to_green(_make_bars([10.0]), prev_close=10.00)
        assert ok is False


class TestDetectFlatTopBreakout:
    def test_triggered_two_equal_highs_then_break(self):
        # Build bars where recent highs cluster near 10.50, then last close breaks above
        highs = [10.0, 10.50, 10.51, 10.49, 10.50, 10.51, 10.49, 10.50, 10.50, 10.50, 10.80]
        closes = [h - 0.05 for h in highs]
        closes[-1] = 10.80
        lows = [c - 0.10 for c in closes]
        bars = _make_bars(closes, highs=highs, lows=lows)
        ok, name, trigger, _ = detect_flat_top_breakout(bars)
        assert ok is True

    def test_not_triggered_no_flat_top(self):
        closes = [10.0, 10.5, 11.0, 11.5, 12.0]
        bars = _make_bars(closes)
        ok, *_ = detect_flat_top_breakout(bars)
        assert ok is False

    def test_needs_at_least_5_bars(self):
        ok, *_ = detect_flat_top_breakout(_make_bars([10.0] * 4))
        assert ok is False


class TestRunSetupDetectors:
    def test_empty_bars_returns_empty(self):
        results = run_setup_detectors(pd.DataFrame(), None, prev_close=10.0)
        assert results == []

    def test_none_pm_levels_returns_empty(self):
        results = run_setup_detectors(_make_bars([10.0, 11.0]), None, prev_close=9.0)
        assert results == []

    def test_returns_list_of_tuples(self):
        bars = _make_bars([10.0, 11.0, 12.0])
        levels = PremkLevels(
            premarket_high=10.5, premarket_low=9.5,
            vwap=10.0, pivots=[], flags=[],
        )
        results = run_setup_detectors(bars, levels, prev_close=9.0)
        assert isinstance(results, list)
        for item in results:
            triggered, name, trigger_price, stop_ref = item
            assert isinstance(triggered, bool)
            assert isinstance(name, str)


class TestFilterResult:
    def test_summary_no_failures(self):
        fr = FilterResult(passed=True, price=10.0, change_pct=15.0, rvol=7.5,
                          float_shares=5_000_000, market_cap=50_000_000)
        summary = fr.summary()
        assert "$10.00" in summary
        assert "15.0%" in summary

    def test_summary_with_failures(self):
        fr = FilterResult(passed=False, price=1.5, change_pct=5.0, rvol=1.2,
                          failures=["price below min", "rvol too low"])
        summary = fr.summary()
        assert "price below min" in summary
        assert "rvol too low" in summary
