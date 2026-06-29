"""Tests for config.py — defaults, validation, and structure."""
import pytest
from config import (
    AlpacaConfig, Config, FilterConfig, NewsEvaluatorConfig,
    RiskConfig, ScannerConfig,
)


class TestAlpacaConfig:
    def test_defaults_are_empty_strings_without_env(self, monkeypatch):
        monkeypatch.delenv("ALPACA_API_KEY", raising=False)
        monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
        cfg = AlpacaConfig()
        assert cfg.api_key == ""
        assert cfg.secret_key == ""

    def test_paper_is_always_true(self):
        assert AlpacaConfig().paper is True

    def test_validate_raises_without_keys(self, monkeypatch):
        monkeypatch.delenv("ALPACA_API_KEY", raising=False)
        monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
        with pytest.raises(ValueError, match="ALPACA_API_KEY"):
            AlpacaConfig().validate()

    def test_validate_passes_with_keys(self, monkeypatch):
        monkeypatch.setenv("ALPACA_API_KEY", "abc")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "xyz")
        cfg = AlpacaConfig()
        cfg.validate()  # should not raise


class TestFilterConfig:
    def test_warrior_defaults(self):
        cfg = FilterConfig()
        assert cfg.min_price == 2.0
        assert cfg.max_price == 20.0
        assert cfg.max_float_shares == 10_000_000
        assert cfg.max_market_cap == 2_000_000_000
        assert cfg.min_change_pct == 5.0
        assert cfg.min_rvol == 5.0


class TestRiskConfig:
    def test_defaults(self):
        cfg = RiskConfig()
        assert cfg.position_size_dollars == 500.0
        assert cfg.breakeven_gain_pct == 0.10
        assert cfg.r1_sell_pct == 0.25
        assert cfg.r1_gain_pct == 0.15
        assert cfg.exhaustion_sell_pct == 0.50
        assert cfg.volume_climax_mult == 3.0
        assert cfg.max_daily_loss_pct == 0.10
        assert cfg.entry_fill_timeout_seconds == 15


class TestScannerConfig:
    def test_defaults(self):
        cfg = ScannerConfig()
        assert cfg.scan_start_hour_et == 4
        assert cfg.scan_end_hour_et == 20
        assert cfg.refresh_interval_seconds == 30


class TestNewsEvaluatorConfig:
    def test_model_name(self):
        cfg = NewsEvaluatorConfig()
        assert "claude" in cfg.model
        assert cfg.confidence_threshold == 0.60
        assert cfg.fallback_to_manual is False

    def test_setup_overrides_present(self):
        cfg = NewsEvaluatorConfig()
        assert "premarket_high_break" in cfg.setup_confidence_overrides
        assert "flat_top_breakout" in cfg.setup_confidence_overrides


class TestConfig:
    def test_paper_only_immutable(self):
        with pytest.raises(ValueError, match="paper_only"):
            Config(paper_only=False)

    def test_trading_window(self):
        cfg = Config()
        assert cfg.trading_start_hour_et == 7
        assert cfg.trading_end_hour_et == 11

    def test_nested_defaults(self):
        cfg = Config()
        assert isinstance(cfg.filters, FilterConfig)
        assert isinstance(cfg.risk, RiskConfig)
        assert isinstance(cfg.scanner, ScannerConfig)
        assert isinstance(cfg.news_evaluator, NewsEvaluatorConfig)
