from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from config import Config, RiskConfig

logger = logging.getLogger(__name__)


@dataclass
class TakeProfitTargets:
    t1: float        # 1:1 risk/reward — sell 50% here
    t2: float        # just below next whole/half-dollar — sell 25% here
    t2_psych: float  # the actual psychological level (for logging)


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
        self, account_equity: float, entry_price: float
    ) -> PositionSizing:
        shares = max(1, int(self._cfg.position_size_dollars / entry_price))
        initial_stop = entry_price - self._cfg.initial_stop_offset
        return PositionSizing(
            shares=shares,
            entry_price=entry_price,
            initial_stop=round(initial_stop, 4),
            risk_amount=round(shares * (entry_price - initial_stop), 2),
            position_value=round(shares * entry_price, 2),
        )

    def calculate_targets(
        self, entry_price: float, stop_price: float
    ) -> TakeProfitTargets:
        risk = entry_price - stop_price
        t1 = round(entry_price + risk, 4)

        # Next whole or half dollar strictly above t1
        t2_psych = math.ceil(t1 * 2) / 2
        if t2_psych <= t1:
            t2_psych += 0.5
        t2 = round(t2_psych - 0.03, 2)

        return TakeProfitTargets(t1=t1, t2=t2, t2_psych=t2_psych)

    def is_stop_hit(self, current_price: float, stop_price: float) -> bool:
        return current_price <= stop_price

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
