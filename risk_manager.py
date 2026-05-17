from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from config import Config, RiskConfig

logger = logging.getLogger(__name__)


@dataclass
class TakeProfitTargets:
    t1: float  # 2:1 R/R — sell 50% here, then move stop to breakeven


@dataclass
class PositionSizing:
    shares: int
    entry_price: float
    initial_stop: float
    risk_amount: float
    position_value: float


class RiskManager:
    def __init__(self, config: Config) -> None:
        self._cfg: RiskConfig = config.risk

    def calculate_position_size(
        self,
        account_equity: float,
        entry_price: float,
        atr: Optional[float] = None,
        setup_stop_ref: Optional[float] = None,
    ) -> PositionSizing:
        # Stop priority: explicit setup stop → ATR-based → fixed offset
        if setup_stop_ref is not None and setup_stop_ref < entry_price:
            initial_stop = setup_stop_ref
        elif atr is not None and atr > 0:
            initial_stop = entry_price - max(
                self._cfg.initial_stop_offset,
                atr * self._cfg.atr_multiplier,
            )
        else:
            initial_stop = entry_price - self._cfg.initial_stop_offset

        initial_stop = round(initial_stop, 4)
        risk_per_share = max(entry_price - initial_stop, 0.01)
        shares = max(1, int(self._cfg.position_size_dollars / risk_per_share))
        return PositionSizing(
            shares=shares,
            entry_price=entry_price,
            initial_stop=initial_stop,
            risk_amount=round(shares * risk_per_share, 2),
            position_value=round(shares * entry_price, 2),
        )

    def calculate_targets(
        self, entry_price: float, stop_price: float
    ) -> TakeProfitTargets:
        risk = entry_price - stop_price
        t1 = round(entry_price + self._cfg.min_rr_ratio * risk, 4)
        return TakeProfitTargets(t1=t1)

    def is_stop_hit(self, current_price: float, stop_price: float) -> bool:
        return current_price <= stop_price

    def is_red_candle(self, bar: pd.Series) -> bool:
        return float(bar["close"]) < float(bar["open"])

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
