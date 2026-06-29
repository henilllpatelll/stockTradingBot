from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from config import Config, RiskConfig

logger = logging.getLogger(__name__)


@dataclass
class PositionSizing:
    shares: int
    entry_price: float
    initial_stop: float
    risk_amount: float
    position_value: float


@dataclass
class Targets:
    t1: float   # first sell target (R1 level — 15% gain)


class RiskManager:
    def __init__(self, config: Config) -> None:
        self._cfg: RiskConfig = config.risk

    def calculate_position_size(
        self,
        account_equity: float,
        entry_price: float,
        entry_bar_low: Optional[float] = None,
        setup_stop_ref: Optional[float] = None,
    ) -> PositionSizing:
        # Stop priority (Warrior Trading — structural price levels only):
        # 1. Setup stop ref (flag bottom, consolidation low, detected pattern level)
        # 2. Entry candle low (Ross's default: stop = low of the entry bar)
        # 3. Fixed minimum offset (last resort when no bars available)
        if setup_stop_ref is not None and setup_stop_ref < entry_price:
            initial_stop = setup_stop_ref
        elif entry_bar_low is not None and entry_bar_low < entry_price:
            initial_stop = entry_bar_low
        else:
            initial_stop = entry_price - self._cfg.initial_stop_offset

        initial_stop = round(initial_stop, 4)
        risk_per_share = max(entry_price - initial_stop, 0.01)
        shares_by_risk = int(self._cfg.position_size_dollars / risk_per_share)
        shares_by_value = int(self._cfg.max_position_dollars / entry_price)
        shares = max(1, min(shares_by_risk, shares_by_value))
        return PositionSizing(
            shares=shares,
            entry_price=entry_price,
            initial_stop=initial_stop,
            risk_amount=round(shares * risk_per_share, 2),
            position_value=round(shares * entry_price, 2),
        )

    def calculate_targets(self, entry_price: float, initial_stop: float) -> Targets:
        return Targets(t1=self.r1_price(entry_price))

    def breakeven_price(self, entry_price: float) -> float:
        return round(entry_price * (1 + self._cfg.breakeven_gain_pct), 4)

    def r1_price(self, entry_price: float) -> float:
        return round(entry_price * (1 + self._cfg.r1_gain_pct), 4)

    def is_volume_climax(self, bar: pd.Series, entry_bar_volume: float) -> bool:
        return float(bar["volume"]) > entry_bar_volume * self._cfg.volume_climax_mult

    def is_big_red_near_low(self, bar: pd.Series, atr: Optional[float] = None) -> bool:
        """Red candle with significant body that closes in the bottom quarter of its range."""
        close = float(bar["close"])
        open_ = float(bar["open"])
        low = float(bar["low"])
        high = float(bar["high"])
        if close >= open_:
            return False
        if atr is not None and (open_ - close) < atr * self._cfg.red_candle_body_atr_mult:
            return False
        candle_range = high - low
        if candle_range <= 0:
            return False
        return (close - low) / candle_range <= 0.25

    def is_near_whole_half_dollar(self, price: float) -> bool:
        proximity = self._cfg.whole_dollar_proximity
        frac = price % 1.0
        near_whole = frac >= (1.0 - proximity) or frac <= proximity
        near_half = abs(frac - 0.5) <= proximity
        return near_whole or near_half

    def is_vwap_break(self, bar: pd.Series, vwap: float) -> bool:
        return float(bar["close"]) < vwap

    def is_stop_hit(self, current_price: float, stop_price: float) -> bool:
        return current_price <= stop_price

    def is_red_candle(self, bar: pd.Series, atr: Optional[float] = None) -> bool:
        close = float(bar["close"])
        open_ = float(bar["open"])
        if close >= open_:
            return False
        if atr is None:
            return True
        body = open_ - close
        return body >= atr * self._cfg.red_candle_body_atr_mult

    def is_extension_bar(self, bar: pd.Series, atr: float) -> bool:
        candle_range = float(bar["high"]) - float(bar["low"])
        return candle_range > atr * self._cfg.extension_bar_atr_mult

    def is_stall(
        self,
        bars_since_entry: pd.DataFrame,
        entry_price: float,
        t1_price: float,
    ) -> bool:
        if len(bars_since_entry) < self._cfg.stall_bars:
            return False
        required_progress = self._cfg.stall_progress_threshold * (t1_price - entry_price)
        best_advance = float(bars_since_entry["high"].max()) - entry_price
        return best_advance < required_progress

    def is_momentum_fail(
        self,
        bars: pd.DataFrame,
        entry_bar_volume: float,
    ) -> bool:
        recent = bars.tail(self._cfg.momentum_fail_bars)
        if len(recent) < self._cfg.momentum_fail_bars:
            return False
        threshold = entry_bar_volume * self._cfg.momentum_fail_vol_ratio
        return bool((recent["volume"] < threshold).all())

    def check_max_daily_loss(
        self, starting_equity: float, current_equity: float
    ) -> bool:
        drawdown = (starting_equity - current_equity) / starting_equity
        if drawdown >= self._cfg.max_daily_loss_pct:
            logger.warning(
                "Max daily loss reached: %.1f%% >= %.1f%%",
                drawdown * 100,
                self._cfg.max_daily_loss_pct * 100,
            )
            return False
        return True
