"""
Premarket Catalyst Strategy
----------------------------
Screens for low-float, small-cap tickers gapping up >5 % on volume
before the regular session opens, then confirms entry via a volume-
acceleration and price-strength check on the most recent minute bars.

Filters applied (all must pass):
  • Price     : $2 – $20
  • Float     : < 20 M shares  (yfinance)
  • Market cap: < $2 B         (yfinance)
  • Change    : > 5 % vs. previous close
  • RVOL      : > 1.0  (today's premarket volume vs. daily-average rate)

Entry confirmation (after filters):
  • Volume acceleration  ≥ 1.2×  (last-5-bar avg vs. prior-25-bar avg)
  • Price making higher lows in the most recent 5 bars
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import yfinance as yf

from alpaca_client import AlpacaClient
from config import Config, FilterConfig

logger = logging.getLogger(__name__)

# Minimum volume-acceleration ratio required for entry confirmation
_MIN_VOL_ACCEL = 1.2


@dataclass
class FilterResult:
    passed: bool
    price: float = 0.0
    prev_close: float = 0.0
    change_pct: float = 0.0
    rvol: float = 0.0
    float_shares: float = 0.0
    market_cap: float = 0.0
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"  price      = ${self.price:.2f}",
            f"  change     = {self.change_pct:+.1f}%",
            f"  RVOL       = {self.rvol:.2f}",
            f"  float      = {self.float_shares / 1e6:.1f} M" if self.float_shares else "  float      = N/A",
            f"  market cap = ${self.market_cap / 1e9:.2f} B" if self.market_cap else "  market cap = N/A",
        ]
        if self.failures:
            lines.append("  FAILURES:")
            for f in self.failures:
                lines.append(f"    ✗ {f}")
        return "\n".join(lines)


@dataclass
class EntrySignal:
    symbol: str
    entry_price: float
    reason: str
    rvol: float
    change_pct: float
    vol_acceleration: float


class PremarketCatalystStrategy:
    def __init__(self, config: Config, client: AlpacaClient) -> None:
        self._cfg: FilterConfig = config.filters
        self._client = client

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    def run_filters(self, symbol: str) -> FilterResult:
        result = FilterResult(passed=False)
        failures: list[str] = []

        # --- live quote ------------------------------------------------
        try:
            quote = self._client.get_latest_quote(symbol)
            price = quote["mid"]
            result.price = price
        except Exception as exc:
            failures.append(f"quote unavailable: {exc}")
            result.failures = failures
            return result

        if not (self._cfg.min_price <= price <= self._cfg.max_price):
            failures.append(
                f"price ${price:.2f} outside range "
                f"${self._cfg.min_price}–${self._cfg.max_price}"
            )

        # --- fundamentals via yfinance ---------------------------------
        try:
            info = yf.Ticker(symbol).info
        except Exception as exc:
            failures.append(f"fundamental data unavailable: {exc}")
            result.failures = failures
            return result

        float_shares: float = info.get("floatShares") or info.get("sharesOutstanding") or 0
        market_cap: float = info.get("marketCap") or 0
        prev_close: float = (
            info.get("previousClose")
            or info.get("regularMarketPreviousClose")
            or 0
        )

        result.float_shares = float_shares
        result.market_cap = market_cap
        result.prev_close = prev_close

        if float_shares and float_shares > self._cfg.max_float_shares:
            failures.append(
                f"float {float_shares / 1e6:.1f} M > "
                f"{self._cfg.max_float_shares / 1e6:.0f} M limit"
            )

        if market_cap and market_cap > self._cfg.max_market_cap:
            failures.append(
                f"market cap ${market_cap / 1e9:.2f} B > "
                f"${self._cfg.max_market_cap / 1e9:.0f} B limit"
            )

        if prev_close and prev_close > 0:
            change_pct = (price - prev_close) / prev_close * 100
            result.change_pct = change_pct
            if change_pct < self._cfg.min_change_pct:
                failures.append(
                    f"change {change_pct:.1f}% < {self._cfg.min_change_pct:.0f}% minimum"
                )
        else:
            failures.append("previous close unavailable — cannot calculate change %")

        # --- RVOL ------------------------------------------------------
        rvol = self._calculate_rvol(symbol)
        result.rvol = rvol
        if rvol < self._cfg.min_rvol:
            failures.append(
                f"RVOL {rvol:.2f} < {self._cfg.min_rvol:.1f} minimum"
            )

        result.failures = failures
        result.passed = len(failures) == 0
        return result

    def check_entry_signal(
        self, symbol: str, filter_result: FilterResult
    ) -> Optional[EntrySignal]:
        """Return an EntrySignal when volume is accelerating and price is trending up."""
        if not filter_result.passed:
            return None

        try:
            bars = self._client.get_intraday_bars(symbol, lookback_hours=6)
        except Exception as exc:
            logger.error("Failed to fetch intraday bars for %s: %s", symbol, exc)
            return None

        if bars.empty or len(bars) < 6:
            logger.warning("%s: not enough intraday bars (%d)", symbol, len(bars))
            return None

        recent = bars.tail(5)
        prior = bars.iloc[:-5] if len(bars) > 5 else bars

        recent_vol_avg = recent["volume"].mean()
        prior_vol_avg = prior["volume"].mean() if not prior.empty else recent_vol_avg
        vol_acceleration = (
            recent_vol_avg / prior_vol_avg if prior_vol_avg > 0 else 1.0
        )

        # Price strength: recent lows are non-decreasing
        recent_lows = recent["low"].values
        price_strength = bool(
            recent_lows[-1] >= recent_lows[:-1].mean()
        )

        entry_price = float(bars["close"].iloc[-1])

        logger.info(
            "%s: vol_accel=%.2fx price_strength=%s entry=$%.2f",
            symbol,
            vol_acceleration,
            price_strength,
            entry_price,
        )

        if vol_acceleration >= _MIN_VOL_ACCEL and price_strength:
            reason = (
                f"vol_accel={vol_acceleration:.2f}x, "
                f"change={filter_result.change_pct:+.1f}%, "
                f"RVOL={filter_result.rvol:.2f}"
            )
            return EntrySignal(
                symbol=symbol,
                entry_price=entry_price,
                reason=reason,
                rvol=filter_result.rvol,
                change_pct=filter_result.change_pct,
                vol_acceleration=vol_acceleration,
            )

        logger.info(
            "%s: entry not confirmed — vol_accel=%.2fx (need ≥%.1fx), price_strength=%s",
            symbol,
            vol_acceleration,
            _MIN_VOL_ACCEL,
            price_strength,
        )
        return None

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    def _calculate_rvol(self, symbol: str) -> float:
        """
        Relative volume = today's premarket volume / expected volume over the
        same elapsed time, based on the 20-day average daily volume.

        Returns 0.0 on any data error — callers treat 0.0 as a filter failure.
        """
        try:
            daily_bars = self._client.get_daily_bars(symbol, days=20)
            if daily_bars.empty or len(daily_bars) < 5:
                logger.warning("%s: too few daily bars for RVOL", symbol)
                return 0.0
            avg_daily_vol = float(daily_bars["volume"].mean())
            if avg_daily_vol <= 0:
                return 0.0

            intraday = self._client.get_intraday_bars(symbol, lookback_hours=6)
            if intraday.empty:
                return 0.0

            today = pd.Timestamp.now(tz="UTC").normalize()
            idx = intraday.index
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
            else:
                idx = idx.tz_convert("UTC")
            today_bars = intraday[idx >= today]

            if today_bars.empty:
                return 0.0

            today_vol = float(today_bars["volume"].sum())
            elapsed_minutes = len(today_bars)

            # How much volume we'd expect in `elapsed_minutes` at the average daily rate
            # (390 regular trading minutes per day used as the base)
            expected = avg_daily_vol / 390 * elapsed_minutes
            return today_vol / expected if expected > 0 else 0.0

        except Exception as exc:
            logger.warning("RVOL calculation failed for %s: %s", symbol, exc)
            return 0.0
