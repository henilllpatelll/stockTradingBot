"""Tests for scanner.py — time gates, catalyst checker, and filter logic (no Alpaca calls)."""
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch
import pytz

from scanner import (
    CatalystChecker,
    GapperCandidate,
    PremarketScanner,
    is_alert_window,
    is_scanner_hours,
    is_trading_hours,
)
from config import Config
from edgar import EdgarSummary

_ET = pytz.timezone("America/New_York")


def _et(hour: int, minute: int = 0) -> datetime:
    return _ET.localize(datetime(2024, 1, 15, hour, minute, 0))


class TestTimeGates:
    def _cfg(self):
        return Config()

    def test_scanner_open_at_4am(self):
        with patch("scanner._et_now", return_value=_et(4, 0)):
            assert is_scanner_hours(self._cfg()) is True

    def test_scanner_closed_before_4am(self):
        with patch("scanner._et_now", return_value=_et(3, 59)):
            assert is_scanner_hours(self._cfg()) is False

    def test_scanner_closed_at_8pm(self):
        with patch("scanner._et_now", return_value=_et(20, 0)):
            assert is_scanner_hours(self._cfg()) is False

    def test_scanner_open_at_noon(self):
        with patch("scanner._et_now", return_value=_et(12, 0)):
            assert is_scanner_hours(self._cfg()) is True

    def test_trading_open_at_9am(self):
        with patch("scanner._et_now", return_value=_et(9, 0)):
            assert is_trading_hours(self._cfg()) is True

    def test_trading_closed_at_6am(self):
        with patch("scanner._et_now", return_value=_et(6, 0)):
            assert is_trading_hours(self._cfg()) is False

    def test_trading_closed_at_end_boundary(self):
        # trading_end_hour_et=11 is exclusive
        with patch("scanner._et_now", return_value=_et(11, 0)):
            assert is_trading_hours(self._cfg()) is False

    def test_trading_open_at_start_boundary(self):
        with patch("scanner._et_now", return_value=_et(7, 0)):
            assert is_trading_hours(self._cfg()) is True

    def test_alert_window_at_7am(self):
        with patch("scanner._et_now", return_value=_et(7, 0)):
            in_alert, hour = is_alert_window(self._cfg())
        assert in_alert is True
        assert hour == 7

    def test_alert_window_at_9am(self):
        with patch("scanner._et_now", return_value=_et(9, 0)):
            in_alert, hour = is_alert_window(self._cfg())
        assert in_alert is True
        assert hour == 9

    def test_no_alert_at_2_minutes_past(self):
        with patch("scanner._et_now", return_value=_et(9, 2)):
            in_alert, _ = is_alert_window(self._cfg())
        assert in_alert is False

    def test_no_alert_outside_windows(self):
        with patch("scanner._et_now", return_value=_et(6, 30)):
            in_alert, _ = is_alert_window(self._cfg())
        assert in_alert is False


class TestCatalystChecker:
    def _checker(self):
        cfg = Config()
        client = MagicMock()
        return CatalystChecker(cfg.scanner, client)

    def test_dilution_item_blocks_trade(self):
        checker = self._checker()
        edgar = EdgarSummary(symbol="TEST", has_dilution_risk=True,
                             summary_lines=["[8-K DILUTION RISK] Item 3.02"])
        with patch.object(checker, "_fetch_edgar", return_value=edgar):
            ok, headline = checker.has_catalyst("TEST", ["Some news"])
        assert ok is False
        assert headline == ""

    def test_headline_used_when_no_dilution(self):
        checker = self._checker()
        edgar = EdgarSummary(symbol="TEST")
        with patch.object(checker, "_fetch_edgar", return_value=edgar):
            ok, headline = checker.has_catalyst("TEST", ["Earnings beat!"])
        assert ok is True
        assert headline == "Earnings beat!"

    def test_first_headline_used(self):
        checker = self._checker()
        edgar = EdgarSummary(symbol="TEST")
        with patch.object(checker, "_fetch_edgar", return_value=edgar):
            ok, headline = checker.has_catalyst("TEST", ["First headline", "Second headline"])
        assert headline == "First headline"

    def test_edgar_positive_fallback_when_no_headlines(self):
        checker = self._checker()
        edgar = EdgarSummary(symbol="TEST", has_positive_catalyst=True,
                             summary_lines=["[8-K CATALYST] 2024-01-15: Items 1.01"])
        with patch.object(checker, "_fetch_edgar", return_value=edgar):
            ok, headline = checker.has_catalyst("TEST", [])
        assert ok is True
        assert "8-K" in headline

    def test_no_catalyst_no_edgar_activity(self):
        checker = self._checker()
        edgar = EdgarSummary(symbol="TEST")
        with patch.object(checker, "_fetch_edgar", return_value=edgar):
            ok, headline = checker.has_catalyst("TEST", [])
        assert ok is False
        assert headline == ""

    def test_dilution_overrides_headlines(self):
        checker = self._checker()
        edgar = EdgarSummary(symbol="TEST", has_dilution_risk=True,
                             has_positive_catalyst=True,
                             summary_lines=["[8-K DILUTION RISK] Item 3.02"])
        with patch.object(checker, "_fetch_edgar", return_value=edgar):
            ok, _ = checker.has_catalyst("TEST", ["Great earnings!"])
        assert ok is False

    def test_fetch_headlines_returns_empty_on_error(self):
        checker = self._checker()
        checker._client.get_news.side_effect = Exception("API down")
        result = checker.fetch_headlines("TEST", 12)
        assert result == []

    def test_fetch_headlines_passes_through_list(self):
        checker = self._checker()
        checker._client.get_news.return_value = ["headline A", "headline B"]
        result = checker.fetch_headlines("TEST", 12)
        assert result == ["headline A", "headline B"]

    def test_fetch_edgar_never_raises(self):
        checker = self._checker()
        with patch("scanner._edgar_summarize", side_effect=Exception("EDGAR down")):
            result = checker._fetch_edgar("TEST")
        assert isinstance(result, EdgarSummary)
        assert result.symbol == "TEST"


class TestPremarketScannerFilters:
    def _scanner(self):
        cfg = Config()
        client = MagicMock()
        return PremarketScanner(cfg, client), cfg

    def _fund(self, **overrides):
        base = {
            "float_shares": 5_000_000,
            "market_cap": 500_000_000,
            "avg_volume": 1_000_000,
            "premarket_price": 8.00,
            "prev_close": 7.00,
            "premarket_volume": 5_000_000,
        }
        base.update(overrides)
        return base

    def test_passes_all_filters(self):
        scanner, _ = self._scanner()
        with patch.object(scanner, "_get_fundamentals", return_value=self._fund()):
            result = scanner._check_symbol("TEST")
        assert result is not None
        assert result.symbol == "TEST"
        assert result.gap_pct == pytest.approx((8.0 - 7.0) / 7.0 * 100, abs=0.01)

    def test_candidate_rvol_calculated(self):
        scanner, _ = self._scanner()
        with patch.object(scanner, "_get_fundamentals", return_value=self._fund()):
            result = scanner._check_symbol("TEST")
        assert result is not None
        # 5_000_000 vol / 1_000_000 avg = 5.0x
        assert result.rvol == pytest.approx(5.0, abs=0.01)

    def test_fails_price_too_low(self):
        scanner, _ = self._scanner()
        with patch.object(scanner, "_get_fundamentals",
                          return_value=self._fund(premarket_price=1.50)):
            assert scanner._check_symbol("TEST") is None

    def test_fails_price_too_high(self):
        scanner, _ = self._scanner()
        with patch.object(scanner, "_get_fundamentals",
                          return_value=self._fund(premarket_price=25.00, prev_close=20.00)):
            assert scanner._check_symbol("TEST") is None

    def test_fails_float_too_large(self):
        scanner, _ = self._scanner()
        with patch.object(scanner, "_get_fundamentals",
                          return_value=self._fund(float_shares=15_000_000)):
            assert scanner._check_symbol("TEST") is None

    def test_fails_float_zero(self):
        scanner, _ = self._scanner()
        with patch.object(scanner, "_get_fundamentals",
                          return_value=self._fund(float_shares=0)):
            assert scanner._check_symbol("TEST") is None

    def test_fails_market_cap_too_large(self):
        scanner, _ = self._scanner()
        with patch.object(scanner, "_get_fundamentals",
                          return_value=self._fund(market_cap=3_000_000_000)):
            assert scanner._check_symbol("TEST") is None

    def test_passes_when_market_cap_zero(self):
        scanner, _ = self._scanner()
        with patch.object(scanner, "_get_fundamentals",
                          return_value=self._fund(market_cap=0)):
            result = scanner._check_symbol("TEST")
        assert result is not None

    def test_fails_rvol_too_low(self):
        scanner, _ = self._scanner()
        # 100k / 1M = 0.1x < 5x minimum
        with patch.object(scanner, "_get_fundamentals",
                          return_value=self._fund(premarket_volume=100_000)):
            assert scanner._check_symbol("TEST") is None

    def test_passes_when_premarket_volume_zero(self):
        # No premarket volume data → RVOL gate skipped
        scanner, _ = self._scanner()
        with patch.object(scanner, "_get_fundamentals",
                          return_value=self._fund(premarket_volume=0)):
            result = scanner._check_symbol("TEST")
        assert result is not None

    def test_fails_gap_too_small(self):
        scanner, _ = self._scanner()
        with patch.object(scanner, "_get_fundamentals",
                          return_value=self._fund(premarket_price=7.35, prev_close=7.00)):
            assert scanner._check_symbol("TEST") is None

    def test_fails_no_price_data(self):
        scanner, _ = self._scanner()
        with patch.object(scanner, "_get_fundamentals",
                          return_value=self._fund(premarket_price=0)):
            assert scanner._check_symbol("TEST") is None

    def test_fails_no_prev_close(self):
        scanner, _ = self._scanner()
        with patch.object(scanner, "_get_fundamentals",
                          return_value=self._fund(prev_close=0)):
            assert scanner._check_symbol("TEST") is None

    def test_scan_once_sorted_by_rvol_desc(self):
        scanner, _ = self._scanner()
        fund_low = self._fund(premarket_volume=5_000_000, avg_volume=1_000_000)   # 5x
        fund_high = self._fund(premarket_volume=15_000_000, avg_volume=1_000_000)  # 15x

        def fake_get_fundamentals(symbol):
            return fund_high if symbol == "HIGH" else fund_low

        with patch.object(scanner, "_get_fundamentals", side_effect=fake_get_fundamentals):
            results = scanner.scan_once(["LOW", "HIGH"])

        assert len(results) == 2
        assert results[0].symbol == "HIGH"
        assert results[1].symbol == "LOW"

    def test_scan_once_empty_universe(self):
        scanner, _ = self._scanner()
        results = scanner.scan_once([])
        assert results == []

    def test_check_symbol_swallows_exception(self):
        scanner, _ = self._scanner()
        with patch.object(scanner, "_get_fundamentals", side_effect=Exception("crash")):
            result = scanner._check_symbol("TEST")
        assert result is None
