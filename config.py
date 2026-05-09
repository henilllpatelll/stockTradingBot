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
    min_price: float = 2.0
    max_price: float = 20.0
    # Maximum float (shares available to trade)
    max_float_shares: float = 20_000_000
    # Maximum market capitalisation
    max_market_cap: float = 2_000_000_000
    # Minimum % change from the previous session's close
    min_change_pct: float = 5.0
    # Minimum relative volume (1.0 = average)
    min_rvol: float = 1.0


@dataclass
class RiskConfig:
    # Max fraction of account equity allocated to one trade
    max_position_pct: float = 0.50
    # Hard stop loss below entry (e.g. 0.07 = 7%)
    stop_loss_pct: float = 0.07
    # Trailing stop that follows the price upward (e.g. 0.05 = 5%)
    trailing_stop_pct: float = 0.05
    # Daily loss ceiling — no new trades once hit
    max_daily_loss_pct: float = 0.10


@dataclass
class Config:
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    log_dir: str = "logs"
    # Safety guard — changing this to False raises immediately.
    paper_only: bool = True
    # Seconds between position-monitoring polls
    monitor_interval_seconds: int = 1.5
    # Force-close position after this many minutes regardless of P&L
    max_hold_minutes: int = 120

    def __post_init__(self) -> None:
        if not self.paper_only:
            raise ValueError(
                "Live trading is not supported. paper_only must remain True."
            )
