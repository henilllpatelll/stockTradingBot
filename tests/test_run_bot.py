"""Tests for run_bot.py helper functions — no Alpaca API calls."""
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch
from datetime import datetime
from types import SimpleNamespace
import pytz

from run_bot import (
    validate_ticker,
    _clamped_ratio,
    _compute_atr,
    _entry_signal_quality,
    _is_trading_hours,
    _learned_entry_gate,
    _now_iso,
    _rank_setups_by_learning,
    _resolve_news_catalyst,
    _AutoArgs,
    run,
)
from config import Config
from strategy import EntrySignal


class TestClampedRatio:
    def test_at_full_strength(self):
        assert _clamped_ratio(10.0, 10.0) == pytest.approx(1.0)

    def test_at_half_strength(self):
        assert _clamped_ratio(5.0, 10.0) == pytest.approx(0.5)

    def test_clamped_below_zero(self):
        assert _clamped_ratio(-5.0, 10.0) == pytest.approx(0.0)

    def test_clamped_above_one(self):
        assert _clamped_ratio(20.0, 10.0) == pytest.approx(1.0)

    def test_zero_full_strength_returns_zero(self):
        assert _clamped_ratio(5.0, 0.0) == pytest.approx(0.0)


class TestValidateTicker:
    def test_valid_symbols(self):
        assert validate_ticker("aapl") == "AAPL"
        assert validate_ticker(" tsla ") == "TSLA"
        assert validate_ticker("GME") == "GME"
        assert validate_ticker("A") == "A"

    def test_too_long(self):
        with pytest.raises(ValueError):
            validate_ticker("TOOLONG")

    def test_contains_digits(self):
        with pytest.raises(ValueError):
            validate_ticker("AA1")

    def test_empty(self):
        with pytest.raises(ValueError):
            validate_ticker("")

    def test_special_chars(self):
        with pytest.raises(ValueError):
            validate_ticker("AA-B")


class TestComputeAtr:
    def _bars(self, highs, lows, closes) -> pd.DataFrame:
        return pd.DataFrame({"high": highs, "low": lows, "close": closes})

    def test_none_on_no_bars(self):
        assert _compute_atr(None) is None

    def test_none_on_single_bar(self):
        bars = self._bars([10.5], [9.5], [10.0])
        assert _compute_atr(bars) is None

    def test_returns_positive_float(self):
        highs =  [10.5, 11.0, 10.8, 11.2, 10.9]
        lows =   [9.5,  10.0, 9.8,  10.2, 9.9]
        closes = [10.0, 10.5, 10.2, 10.8, 10.3]
        result = _compute_atr(self._bars(highs, lows, closes))
        assert result is not None
        assert result > 0

    def test_atr_uses_true_range(self):
        # Gap up: prev close 10, today low 11 — TR should include gap
        highs  = [10.0, 12.0]
        lows   = [9.0,  11.0]
        closes = [10.0, 11.5]
        result = _compute_atr(self._bars(highs, lows, closes))
        # TR for bar 2: max(12-11, |12-10|, |11-10|) = max(1, 2, 1) = 2.0
        # ATR rolling mean of [1.0, 2.0] = 1.5
        assert result is not None
        assert result > 1.0  # must capture the gap


class TestIsTradingHours:
    def _mock_time(self, hour: int):
        et = pytz.timezone("America/New_York")
        dt = datetime(2024, 1, 15, hour, 0, 0)
        return et.localize(dt)

    def test_inside_window(self):
        config = Config()
        et = pytz.timezone("America/New_York")
        with patch("run_bot.datetime") as mock_dt:
            mock_dt.now.return_value = et.localize(datetime(2024, 1, 15, 9, 0, 0))
            result = _is_trading_hours(config)
        assert result is True

    def test_before_window(self):
        config = Config()
        et = pytz.timezone("America/New_York")
        with patch("run_bot.datetime") as mock_dt:
            mock_dt.now.return_value = et.localize(datetime(2024, 1, 15, 6, 0, 0))
            result = _is_trading_hours(config)
        assert result is False

    def test_after_window(self):
        config = Config()
        et = pytz.timezone("America/New_York")
        with patch("run_bot.datetime") as mock_dt:
            mock_dt.now.return_value = et.localize(datetime(2024, 1, 15, 12, 0, 0))
            result = _is_trading_hours(config)
        assert result is False

    def test_at_start_boundary(self):
        config = Config()
        et = pytz.timezone("America/New_York")
        with patch("run_bot.datetime") as mock_dt:
            mock_dt.now.return_value = et.localize(datetime(2024, 1, 15, 7, 0, 0))
            result = _is_trading_hours(config)
        assert result is True

    def test_at_end_boundary(self):
        # trading_end_hour_et=11 → hour 11 is OUT (exclusive)
        config = Config()
        et = pytz.timezone("America/New_York")
        with patch("run_bot.datetime") as mock_dt:
            mock_dt.now.return_value = et.localize(datetime(2024, 1, 15, 11, 0, 0))
            result = _is_trading_hours(config)
        assert result is False


class TestNowIso:
    def test_returns_string(self):
        result = _now_iso()
        assert isinstance(result, str)

    def test_is_iso_format(self):
        result = _now_iso()
        # Should be parseable as ISO 8601
        from datetime import datetime as dt
        parsed = dt.fromisoformat(result)
        assert parsed is not None

    def test_includes_timezone(self):
        result = _now_iso()
        assert "+" in result or result.endswith("Z")


class TestLearningHelpers:
    def test_rank_setups_prefers_sampled_higher_win_rate(self):
        active_setups = [
            ("premarket_high_break", 10.5, None),
            ("pm_flag_break", 10.7, 10.1),
            ("flat_top_breakout", 10.9, None),
        ]
        perf_stats = {
            "by_setup": {
                "premarket_high_break": {"trades": 8, "win_rate": 0.38, "avg_pnl": -12.0},
                "pm_flag_break": {"trades": 8, "win_rate": 0.75, "avg_pnl": 31.0},
                "flat_top_breakout": {"trades": 3, "win_rate": 1.0, "avg_pnl": 45.0},
            }
        }

        ranked = _rank_setups_by_learning(active_setups, perf_stats)

        assert ranked[0][0] == "pm_flag_break"
        assert ranked[-1][0] == "premarket_high_break"

    def test_rank_setups_keeps_order_without_enough_samples(self):
        active_setups = [
            ("premarket_high_break", 10.5, None),
            ("pm_flag_break", 10.7, 10.1),
        ]
        perf_stats = {
            "by_setup": {
                "pm_flag_break": {"trades": 4, "win_rate": 1.0, "avg_pnl": 99.0},
            }
        }

        assert _rank_setups_by_learning(active_setups, perf_stats) == active_setups

    def test_entry_signal_quality_scores_stronger_setups_higher(self):
        config = Config()
        weak = EntrySignal(
            symbol="AAPL",
            entry_price=10.0,
            reason="weak",
            rvol=5.0,
            change_pct=10.0,
            vol_acceleration=1.2,
            setup_name="premarket_high_break",
        )
        strong = EntrySignal(
            symbol="AAPL",
            entry_price=10.0,
            reason="strong",
            rvol=12.0,
            change_pct=35.0,
            vol_acceleration=3.5,
            setup_name="premarket_high_break",
        )

        assert _entry_signal_quality(strong, config) > _entry_signal_quality(weak, config)
        assert 0.0 <= _entry_signal_quality(weak, config) <= 1.0

    def test_learned_entry_gate_blocks_marginal_signal_after_losses(self):
        class LosingTracker:
            def adjusted_threshold(self, setup_name, base):
                assert setup_name == "premarket_high_break"
                assert base == pytest.approx(0.65)
                return 0.80

        config = Config()
        signal = EntrySignal(
            symbol="AAPL",
            entry_price=10.0,
            reason="marginal",
            rvol=5.5,
            change_pct=12.0,
            vol_acceleration=1.3,
            setup_name="premarket_high_break",
        )

        passed, quality, threshold = _learned_entry_gate(signal, config, LosingTracker())

        assert passed is False
        assert quality < threshold

    def test_learned_entry_gate_is_neutral_without_enough_history(self):
        class NeutralTracker:
            def adjusted_threshold(self, setup_name, base):
                return base

        config = Config()
        signal = EntrySignal(
            symbol="AAPL",
            entry_price=10.0,
            reason="minimum",
            rvol=2.0,
            change_pct=5.0,
            vol_acceleration=1.2,
            setup_name="premarket_high_break",
        )

        passed, quality, threshold = _learned_entry_gate(signal, config, NeutralTracker())

        assert passed is True
        assert quality < threshold

    def test_learned_entry_gate_allows_strong_signal_when_threshold_met(self):
        class WinningTracker:
            def adjusted_threshold(self, setup_name, base):
                return 0.58

        config = Config()
        signal = EntrySignal(
            symbol="AAPL",
            entry_price=10.0,
            reason="strong",
            rvol=14.0,
            change_pct=38.0,
            vol_acceleration=3.6,
            setup_name="premarket_high_break",
        )

        passed, quality, threshold = _learned_entry_gate(signal, config, WinningTracker())

        assert passed is True
        assert quality >= threshold


class TestNewsCatalystResolution:
    def test_headline_override_confirms_news_catalyst_without_fetch(self):
        client = MagicMock()
        args = type("Args", (), {"catalyst_headline": "Company wins major contract"})()

        headline, catalyst_type = _resolve_news_catalyst(client, "TEST", Config(), args)

        assert headline == "Company wins major contract"
        assert catalyst_type == "news"
        client.get_news.assert_not_called()

    def test_first_recent_headline_confirms_news_catalyst(self):
        client = MagicMock()
        client.get_news.return_value = ["First catalyst", "Second catalyst"]

        headline, catalyst_type = _resolve_news_catalyst(client, "TEST", Config(), None)

        assert headline == "First catalyst"
        assert catalyst_type == "news"

    def test_no_headlines_returns_no_catalyst(self):
        client = MagicMock()
        client.get_news.return_value = []

        headline, catalyst_type = _resolve_news_catalyst(client, "TEST", Config(), None)

        assert headline == ""
        assert catalyst_type == ""

    def test_run_requires_news_catalyst_for_catalyst_strategy(self):
        config = Config()
        client = MagicMock()
        client.get_account.return_value = SimpleNamespace(
            equity=1000.0,
            last_equity=1000.0,
            buying_power=1000.0,
        )
        client.get_latest_quote.return_value = {"mid": 5.0, "ask": 5.05}
        client.get_quote_with_spread.return_value = {"wide_spread": False, "spread_pct": 0.5}
        client.get_premarket_bars.return_value = pd.DataFrame()
        client.get_intraday_bars.return_value = pd.DataFrame()
        client.get_news.return_value = []
        trade_logger = MagicMock()
        perf_tracker = MagicMock()
        perf_tracker.get_stats.return_value = {}

        with (
            patch("run_bot.AlpacaClient", return_value=client),
            patch("run_bot.TradeLogger", return_value=trade_logger),
            patch("run_bot.PerformanceTracker", return_value=perf_tracker),
            patch("run_bot.PremarketCatalystStrategy"),
            patch("run_bot.RiskManager") as risk_manager,
        ):
            run(
                "TEST",
                config,
                skip_filters=True,
                skip_entry_signal=True,
                args=_AutoArgs(no_trading_hours_gate=True),
                strategy="catalyst",
            )

        client.get_news.assert_called_once_with("TEST", lookback_hours=config.scanner.news_lookback_hours)
        trade_logger.print_summary.assert_called_once()
        risk_manager.return_value.calculate_position_size.assert_not_called()

    def test_news_fetch_error_returns_no_catalyst(self):
        client = MagicMock()
        client.get_news.side_effect = Exception("news down")

        headline, catalyst_type = _resolve_news_catalyst(client, "TEST", Config(), None)

        assert headline == ""
        assert catalyst_type == ""
