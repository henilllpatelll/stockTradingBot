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
    min_price: float = 2.0
    max_price: float = 20.0
    # Maximum float (shares available to trade) — Warrior: under 10M
    max_float_shares: float = 10_000_000
    # Maximum market capitalisation
    max_market_cap: float = 2_000_000_000
    # Minimum % gap from the previous session's close
    min_change_pct: float = 5.0
    # Minimum relative volume (1.0 = average)
    min_rvol: float = 5.0
    # False = scanner strategy (no news headline required)
    require_catalyst: bool = True


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
    # Minimum stop offset when no candle low or setup ref is available
    initial_stop_offset: float = 0.10
    max_daily_loss_pct: float = 0.10
    # Candle range > atr × this = parabolic/extension bar → exhaustion signal
    extension_bar_atr_mult: float = 2.0
    # Red candle body must be > atr × this to count as "big" red candle
    red_candle_body_atr_mult: float = 0.40
    # Bail if price makes < stall_progress_threshold progress toward breakeven after N bars
    stall_bars: int = 3
    stall_progress_threshold: float = 0.10
    # Bail if volume drops below this fraction of entry-bar volume for N consecutive bars
    momentum_fail_vol_ratio: float = 0.50
    momentum_fail_bars: int = 2
    # Hard cap on position value regardless of stop tightness
    max_position_dollars: float = 500.0
    # Breakeven: move stop to entry on first +10% gain (no sell)
    breakeven_gain_pct: float = 0.10
    # R1: sell 25% at first obvious resistance (whole/half dollar or +15% from entry)
    r1_sell_pct: float = 0.25
    r1_gain_pct: float = 0.15
    # Exhaustion: sell 50% of remaining on extension bar or volume climax
    exhaustion_sell_pct: float = 0.50
    volume_climax_mult: float = 3.0       # candle volume > 3× entry bar = climax
    whole_dollar_proximity: float = 0.07  # within $0.07 of X.00 or X.50 = resistance
    # Seconds to wait for limit entry fill before cancelling
    entry_fill_timeout_seconds: int = 15


@dataclass
class GlobeNewswireConfig:
    enabled: bool = True
    feed_url: str = "https://rss.globenewswire.com/rssfeed/"
    poll_interval_seconds: int = 30
    max_seen_guids: int = 500


@dataclass
class NewsEvaluatorConfig:
    enabled: bool = False
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
    # Strategy A — news catalyst required, $2–$20
    filters: FilterConfig = field(default_factory=FilterConfig)
    # Strategy B — no catalyst required, $0–$20, scanner-driven
    technical_filters: FilterConfig = field(default_factory=lambda: FilterConfig(
        min_price=0.0,
        require_catalyst=False,
    ))
    risk: RiskConfig = field(default_factory=RiskConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    news_evaluator: NewsEvaluatorConfig = field(default_factory=NewsEvaluatorConfig)
    globenewswire: GlobeNewswireConfig = field(default_factory=GlobeNewswireConfig)

    # "catalyst" = Strategy A (news-driven), "technical" = Strategy B (scanner-driven), "both" = run both
    active_strategy: str = "both"

    log_dir: str = "logs"
    paper_only: bool = True
    monitor_interval_seconds: int = 1
    max_hold_minutes: int = 30
    # Active execution window — scanner still runs 4-20 ET but trades only fire here
    trading_start_hour_et: int = 7   # 7:00 AM ET
    trading_end_hour_et: int = 11    # 11:00 AM ET

    def __post_init__(self) -> None:
        if not self.paper_only:
            raise ValueError(
                "Live trading is not supported. paper_only must remain True."
            )
