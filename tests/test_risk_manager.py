"""Tests for risk_manager.py — position sizing, targets, and exit conditions."""
import pytest
import pandas as pd
from config import Config
from risk_manager import RiskManager, PositionSizing


@pytest.fixture
def rm():
    return RiskManager(Config())


def _bar(open_: float, high: float, low: float, close: float, volume: float = 1_000) -> pd.Series:
    return pd.Series({"open": open_, "high": high, "low": low, "close": close, "volume": volume})


def _bars(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class TestPositionSizing:
    def test_setup_stop_ref_takes_priority(self, rm):
        sizing = rm.calculate_position_size(10_000, entry_price=10.0, entry_bar_low=9.80, setup_stop_ref=9.70)
        assert sizing.initial_stop == 9.70

    def test_entry_bar_low_used_when_no_setup_stop(self, rm):
        sizing = rm.calculate_position_size(10_000, entry_price=10.0, entry_bar_low=9.85)
        assert sizing.initial_stop == 9.85

    def test_fixed_offset_fallback(self, rm):
        sizing = rm.calculate_position_size(10_000, entry_price=10.0)
        assert sizing.initial_stop == round(10.0 - 0.10, 4)

    def test_shares_floor_is_1(self, rm):
        # Tiny risk per share → huge shares, not zero
        sizing = rm.calculate_position_size(10_000, entry_price=10.0, entry_bar_low=9.99)
        assert sizing.shares >= 1

    def test_setup_stop_above_entry_ignored(self, rm):
        # setup_stop_ref > entry_price must not be used
        sizing = rm.calculate_position_size(10_000, entry_price=10.0, setup_stop_ref=10.50)
        assert sizing.initial_stop < 10.0

    def test_position_value_is_shares_times_entry(self, rm):
        sizing = rm.calculate_position_size(10_000, entry_price=8.0, entry_bar_low=7.80)
        assert abs(sizing.position_value - sizing.shares * sizing.entry_price) < 0.01

    def test_risk_amount_matches_sizing(self, rm):
        sizing = rm.calculate_position_size(10_000, entry_price=10.0, entry_bar_low=9.50)
        expected_risk = sizing.shares * (sizing.entry_price - sizing.initial_stop)
        assert abs(sizing.risk_amount - expected_risk) < 0.01


class TestPriceLevels:
    def test_breakeven_price_at_10pct(self, rm):
        assert rm.breakeven_price(10.0) == pytest.approx(11.0, abs=0.0001)

    def test_r1_price_at_15pct(self, rm):
        assert rm.r1_price(10.0) == pytest.approx(11.5, abs=0.0001)


class TestStopHit:
    def test_stop_hit_at_price(self, rm):
        assert rm.is_stop_hit(current_price=9.50, stop_price=9.50) is True

    def test_stop_hit_below_price(self, rm):
        assert rm.is_stop_hit(current_price=9.00, stop_price=9.50) is True

    def test_stop_not_hit(self, rm):
        assert rm.is_stop_hit(current_price=10.00, stop_price=9.50) is False


class TestCandleConditions:
    def test_red_candle_close_below_open(self, rm):
        bar = _bar(open_=10.0, high=10.5, low=9.5, close=9.8)
        assert rm.is_red_candle(bar) is True

    def test_green_candle_not_red(self, rm):
        bar = _bar(open_=10.0, high=10.5, low=9.5, close=10.3)
        assert rm.is_red_candle(bar) is False

    def test_extension_bar_wide_range(self, rm):
        bar = _bar(open_=10.0, high=12.0, low=9.0, close=11.0)  # range = 3.0
        atr = 1.0  # mult=2.0 → threshold = 2.0, range 3.0 > 2.0
        assert rm.is_extension_bar(bar, atr) is True

    def test_extension_bar_normal_range(self, rm):
        bar = _bar(open_=10.0, high=10.5, low=9.9, close=10.3)  # range = 0.6
        atr = 1.0  # threshold = 2.0
        assert rm.is_extension_bar(bar, atr) is False


class TestVolumeClimax:
    def test_climax_above_3x(self, rm):
        bar = _bar(open_=10.0, high=10.5, low=9.5, close=10.3, volume=3_001)
        assert rm.is_volume_climax(bar, entry_bar_volume=1_000) is True

    def test_no_climax_below_3x(self, rm):
        bar = _bar(open_=10.0, high=10.5, low=9.5, close=10.3, volume=2_999)
        assert rm.is_volume_climax(bar, entry_bar_volume=1_000) is False


class TestBigRedNearLow:
    def test_big_red_in_bottom_quarter(self, rm):
        # range=2.0, close at low → (close-low)/range = 0 ≤ 0.25
        bar = _bar(open_=12.0, high=12.0, low=10.0, close=10.0)
        assert rm.is_big_red_near_low(bar) is True

    def test_red_but_closes_near_high(self, rm):
        # close near top of range → not closing near low
        bar = _bar(open_=12.0, high=12.0, low=10.0, close=11.8)
        assert rm.is_big_red_near_low(bar) is False

    def test_green_candle_not_big_red(self, rm):
        bar = _bar(open_=10.0, high=12.0, low=9.5, close=11.0)
        assert rm.is_big_red_near_low(bar) is False

    def test_small_body_filtered_by_atr(self, rm):
        # body=0.1, atr=1.0, mult=0.4 → threshold=0.4 → body < threshold
        bar = _bar(open_=10.1, high=10.5, low=9.0, close=10.0)
        assert rm.is_big_red_near_low(bar, atr=1.0) is False


class TestStall:
    def test_stall_when_no_progress(self, rm):
        rows = [{"high": 10.05, "low": 9.95, "close": 10.0, "open": 10.0, "volume": 1000}] * 4
        bars = _bars(rows)
        # entry=10.0, t1=11.0 → required_progress = 0.10 * 1.0 = 0.10, best_advance = 0.05
        assert rm.is_stall(bars, entry_price=10.0, t1_price=11.0) is True

    def test_no_stall_when_progress_made(self, rm):
        rows = [
            {"high": 10.5, "low": 10.0, "close": 10.4, "open": 10.1, "volume": 2000},
            {"high": 10.7, "low": 10.3, "close": 10.6, "open": 10.4, "volume": 2000},
            {"high": 10.8, "low": 10.5, "close": 10.7, "open": 10.6, "volume": 2000},
            {"high": 10.9, "low": 10.6, "close": 10.8, "open": 10.7, "volume": 2000},
        ]
        bars = _bars(rows)
        assert rm.is_stall(bars, entry_price=10.0, t1_price=11.0) is False

    def test_no_stall_when_too_few_bars(self, rm):
        bars = _bars([{"high": 10.01, "low": 9.99, "close": 10.0, "open": 10.0, "volume": 500}] * 2)
        assert rm.is_stall(bars, entry_price=10.0, t1_price=11.0) is False


class TestMomentumFail:
    def test_momentum_fail_low_volume(self, rm):
        rows = [
            {"high": 10.5, "low": 10.0, "close": 10.2, "open": 10.1, "volume": 200},
            {"high": 10.4, "low": 10.1, "close": 10.2, "open": 10.2, "volume": 180},
        ]
        bars = _bars(rows)
        # entry_bar_volume=1000, threshold=500; both bars < 500
        assert rm.is_momentum_fail(bars, entry_bar_volume=1_000) is True

    def test_no_momentum_fail_high_volume(self, rm):
        rows = [
            {"high": 10.5, "low": 10.0, "close": 10.2, "open": 10.1, "volume": 800},
            {"high": 10.4, "low": 10.1, "close": 10.2, "open": 10.2, "volume": 900},
        ]
        bars = _bars(rows)
        assert rm.is_momentum_fail(bars, entry_bar_volume=1_000) is False

    def test_momentum_fail_too_few_bars(self, rm):
        bars = _bars([{"high": 10.0, "low": 9.9, "close": 9.95, "open": 10.0, "volume": 100}])
        assert rm.is_momentum_fail(bars, entry_bar_volume=1_000) is False


class TestMaxDailyLoss:
    def test_under_limit_returns_true(self, rm):
        assert rm.check_max_daily_loss(starting_equity=10_000, current_equity=9_200) is True

    def test_at_limit_returns_false(self, rm):
        # 10% drawdown exactly
        assert rm.check_max_daily_loss(starting_equity=10_000, current_equity=9_000) is False

    def test_over_limit_returns_false(self, rm):
        assert rm.check_max_daily_loss(starting_equity=10_000, current_equity=8_000) is False

    def test_no_drawdown_returns_true(self, rm):
        assert rm.check_max_daily_loss(starting_equity=10_000, current_equity=10_000) is True
