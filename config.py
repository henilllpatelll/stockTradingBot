from __future__ import annotations

import os
from dataclasses import dataclass, field

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
    # Fixed dollar amount to spend per trade
    position_size_dollars: float = 500.0
    # Fixed dollar offset below avg fill price for the initial stop
    initial_stop_offset: float = 0.10
    # ATR multiplier for the post-T1 trailing stop buffer (0.5 to 1.0)
    atr_multiplier: float = 0.5
    # Daily loss ceiling — no new trades once hit
    max_daily_loss_pct: float = 0.10


@dataclass
class Config:
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)

    log_dir: str = "logs"
    # Safety guard — changing this to False raises immediately.
    paper_only: bool = True
    # Seconds between position-monitoring polls
    monitor_interval_seconds: int = 1.0
    # Force-close position after this many minutes regardless of P&L
    max_hold_minutes: int = 120
    # Trading session window (ET hours) used by is_trading_hours()
    trading_start_hour_et: int = 4   # 4:00 AM ET — premarket
    trading_end_hour_et: int = 20    # 8:00 PM ET — after-hours close

    def __post_init__(self) -> None:
        if not self.paper_only:
            raise ValueError(
                "Live trading is not supported. paper_only must remain True."
            )
