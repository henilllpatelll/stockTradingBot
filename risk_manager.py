from __future__ import annotations

import logging
from dataclasses import dataclass

from config import Config, RiskConfig

logger = logging.getLogger(__name__)


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
        max_value = account_equity * self._cfg.max_position_pct
        shares = max(1, int(max_value / entry_price))
        initial_stop = entry_price * (1.0 - self._cfg.stop_loss_pct)
        return PositionSizing(
            shares=shares,
            entry_price=entry_price,
            initial_stop=round(initial_stop, 4),
            risk_amount=round(shares * (entry_price - initial_stop), 2),
            position_value=round(shares * entry_price, 2),
        )

    def update_trailing_stop(
        self,
        current_price: float,
        current_stop: float,
    ) -> float:
        """Return the higher of the existing stop and a fresh trail off current price."""
        candidate = current_price * (1.0 - self._cfg.trailing_stop_pct)
        return max(candidate, current_stop)

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
