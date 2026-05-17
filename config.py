from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict

from dotenv import load_dotenv

load_dotenv()


@dataclass
class AlpacaConfig:
    api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    # Hard-coded True — live trading is not supported by this bot.
    paper: bool = True

    def validate(self) -> None:
        if not self.api_key or not self.secret_key:
            raise ValueError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env"
            )


@dataclass
class FilterConfig:
    # Price range for eligible tickers
    min_price: float = 1.0
    max_price: float = 20.0
    # Maximum float (shares available to trade)
    max_float_shares: float = 20_000_000
    # Maximum market capitalisation
    max_market_cap: float = 2_000_000_000
    # Minimum % change from the previous session's close
    min_change_pct: float = 5.0
    # Minimum relative volume (1.0 = average)
    min_rvol: float = 5.0


@dataclass
class ScannerConfig:
    scan_start_hour_et: int = 4   # 4:00 AM ET — premarket open
    scan_end_hour_et: int = 20    # 8:00 PM ET — after-hours close
    scan_end_minute_et: int = 0
    alert_hours_et: list = field(default_factory=lambda: [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19])
    refresh_interval_seconds: int = 30
    news_lookback_hours: int = 12


@dataclass
class RiskConfig:
    position_size_dollars: float = 500.0
    initial_stop_offset: float = 0.10
    atr_multiplier: float = 0.5
    max_daily_loss_pct: float = 0.10
    # Minimum reward/risk ratio — T1 target = entry + min_rr_ratio × risk
    min_rr_ratio: float = 2.0
    # Candle range > atr × this = climax/extension bar → exit signal
    extension_bar_atr_mult: float = 2.0
    # Bail if price makes < stall_progress_threshold progress toward T1 after N bars
    stall_bars: int = 3
    stall_progress_threshold: float = 0.10
    # Bail if volume drops below this fraction of entry-bar volume for N consecutive bars
    momentum_fail_vol_ratio: float = 0.50
    momentum_fail_bars: int = 2
    # Seconds to wait for limit entry fill before cancelling
    entry_fill_timeout_seconds: int = 15


@dataclass
class NewsEvaluatorConfig:
    enabled: bool = True
    model: str = "claude-sonnet-4-6"
    confidence_threshold: float = 0.60
    # Per-setup confidence overrides (higher = stricter for riskier patterns)
    setup_confidence_overrides: Dict[str, float] = field(default_factory=lambda: {
        "premarket_high_break": 0.65,
        "pm_flag_break": 0.60,
        "pm_pivot_break": 0.60,
        "red_to_green": 0.60,
        "1min_orb": 0.65,
        "5min_orb": 0.62,
        "first_pullback_bull_flag": 0.58,
        "flat_top_breakout": 0.70,
    })
    # "hot" = more permissive, "cold" = more selective
    market_regime: str = "hot"
    # False = autonomous (API errors → skip trade, never prompt user)
    fallback_to_manual: bool = False
    timeout_seconds: float = 15.0
    max_tokens: int = 512


@dataclass
class Config:
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    news_evaluator: NewsEvaluatorConfig = field(default_factory=NewsEvaluatorConfig)

    log_dir: str = "logs"
    paper_only: bool = True
    monitor_interval_seconds: int = 1
    max_hold_minutes: int = 120
    # Active execution window — scanner still runs 4-20 ET but trades only fire here
    trading_start_hour_et: int = 7   # 7:00 AM ET
    trading_end_hour_et: int = 11    # 11:00 AM ET

    def __post_init__(self) -> None:
        if not self.paper_only:
            raise ValueError(
                "Live trading is not supported. paper_only must remain True."
            )
