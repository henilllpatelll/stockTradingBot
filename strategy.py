from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Tuple, List

import pandas as pd
import pytz
import yfinance as yf

from alpaca_client import AlpacaClient
from config import Config, FilterConfig

logger = logging.getLogger(__name__)

_MIN_VOL_ACCEL = 1.2
_ET = pytz.timezone("America/New_York")


# ------------------------------------------------------------------ #
# Premarket level structures                                          #
# ------------------------------------------------------------------ #

@dataclass
class PremkLevels:
    premarket_high: float
    premarket_low: float
    vwap: float
    pivots: List[dict]  # {"type": "high"|"low", "price": float, "bar_index": int}
    flags: List[dict]   # {"top": float, "bottom": float, "start_index": int, "end_index": int}


@dataclass
class PatternContext:
    setup_name: str
    pm_high: float
    pm_low: float
    vwap: float
    current_price: float
    price_vs_vwap_pct: float      # (price - vwap) / vwap × 100
    price_vs_pm_high_pct: float   # (price - pm_high) / pm_high × 100
    active_setups: List[str]


# ------------------------------------------------------------------ #
# Premarket level marking                                             #
# ------------------------------------------------------------------ #

def mark_premarket_levels(bars: pd.DataFrame) -> Optional[PremkLevels]:
    if bars is None or bars.empty or len(bars) < 2:
        return None

    pm_high = float(bars["high"].max())
    pm_low = float(bars["low"].min())

    # VWAP: (H+L+C)/3 × volume, summed
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    total_vol = float(bars["volume"].sum())
    vwap = float((typical * bars["volume"]).sum() / total_vol) if total_vol > 0 else pm_high

    # Pivot highs/lows: bar[i] higher than both neighbors
    pivots: List[dict] = []
    highs = bars["high"].values
    lows = bars["low"].values
    for i in range(1, len(bars) - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            pivots.append({"type": "high", "price": float(highs[i]), "bar_index": i})
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            pivots.append({"type": "low", "price": float(lows[i]), "bar_index": i})

    # Flag zones: 4-bar window with < 0.5% range (tight consolidation)
    flags: List[dict] = []
    window = 4
    for i in range(len(bars) - window):
        w = bars.iloc[i:i + window]
        w_high = float(w["high"].max())
        w_low = float(w["low"].min())
        if w_low > 0 and (w_high - w_low) / w_low < 0.005:
            flags.append({
                "top": w_high,
                "bottom": w_low,
                "start_index": i,
                "end_index": i + window - 1,
            })

    return PremkLevels(
        premarket_high=pm_high,
        premarket_low=pm_low,
        vwap=vwap,
        pivots=pivots,
        flags=flags,
    )


# ------------------------------------------------------------------ #
# Setup detectors — each returns (triggered, name, trigger_price, stop_ref)
# ------------------------------------------------------------------ #

def detect_premarket_high_break(
    bars: pd.DataFrame, pm_high: float
) -> Tuple[bool, str, float, Optional[float]]:
    if bars.empty or pm_high <= 0:
        return False, "premarket_high_break", 0.0, None
    triggered = float(bars["close"].iloc[-1]) > pm_high
    return triggered, "premarket_high_break", pm_high, None


def detect_pm_flag_break(
    bars: pd.DataFrame, flags: List[dict]
) -> Tuple[bool, str, float, Optional[float]]:
    if bars.empty or not flags:
        return False, "pm_flag_break", 0.0, None
    latest_flag = flags[-1]
    triggered = float(bars["close"].iloc[-1]) > latest_flag["top"]
    return triggered, "pm_flag_break", latest_flag["top"], latest_flag["bottom"]


def detect_pm_pivot_break(
    bars: pd.DataFrame, pivots: List[dict]
) -> Tuple[bool, str, float, Optional[float]]:
    if bars.empty or not pivots:
        return False, "pm_pivot_break", 0.0, None
    pivot_highs = [p for p in pivots if p["type"] == "high"]
    pivot_lows = [p for p in pivots if p["type"] == "low"]
    if not pivot_highs:
        return False, "pm_pivot_break", 0.0, None
    last_high = pivot_highs[-1]["price"]
    last_low = pivot_lows[-1]["price"] if pivot_lows else None
    triggered = float(bars["close"].iloc[-1]) > last_high
    return triggered, "pm_pivot_break", last_high, last_low


def detect_red_to_green(
    bars: pd.DataFrame, prev_close: float
) -> Tuple[bool, str, float, Optional[float]]:
    if bars.empty or len(bars) < 2 or prev_close <= 0:
        return False, "red_to_green", 0.0, None
    prev_bar_close = float(bars["close"].iloc[-2])
    last_bar_close = float(bars["close"].iloc[-1])
    triggered = prev_bar_close < prev_close and last_bar_close >= prev_close
    stop_ref = round(prev_close * 0.995, 4)
    return triggered, "red_to_green", prev_close, stop_ref


def detect_1min_orb(
    bars: pd.DataFrame, session_open_time: Optional[datetime]
) -> Tuple[bool, str, float, Optional[float]]:
    if bars.empty or session_open_time is None:
        return False, "1min_orb", 0.0, None
    idx = bars.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    open_ts = pd.Timestamp(session_open_time).tz_convert("UTC")
    open_bars = bars[idx == open_ts]
    if open_bars.empty:
        return False, "1min_orb", 0.0, None
    orb_high = float(open_bars["high"].max())
    orb_low = float(open_bars["low"].min())
    last_close = float(bars["close"].iloc[-1])
    triggered = last_close > orb_high and idx[-1] > open_ts
    return triggered, "1min_orb", orb_high, orb_low


def detect_5min_orb(
    bars: pd.DataFrame, session_open_time: Optional[datetime]
) -> Tuple[bool, str, float, Optional[float]]:
    if bars.empty or session_open_time is None:
        return False, "5min_orb", 0.0, None
    idx = bars.index
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    open_ts = pd.Timestamp(session_open_time).tz_convert("UTC")
    five_min_ts = open_ts + pd.Timedelta(minutes=5)
    orb_bars = bars[(idx >= open_ts) & (idx < five_min_ts)]
    if orb_bars.empty:
        return False, "5min_orb", 0.0, None
    orb_high = float(orb_bars["high"].max())
    orb_low = float(orb_bars["low"].min())
    last_close = float(bars["close"].iloc[-1])
    triggered = last_close > orb_high and idx[-1] >= five_min_ts
    return triggered, "5min_orb", orb_high, orb_low


def detect_first_pullback_bull_flag(
    bars: pd.DataFrame,
) -> Tuple[bool, str, float, Optional[float]]:
    if bars.empty or len(bars) < 10:
        return False, "first_pullback_bull_flag", 0.0, None
    # Need: impulse (3+ bars, +3% gain), then consolidation (<0.5% range), then break
    closes = bars["close"].values
    highs = bars["high"].values
    lows = bars["low"].values
    n = len(closes)
    for i in range(3, n - 3):
        impulse_gain = (closes[i] - closes[i - 3]) / closes[i - 3] * 100
        if impulse_gain < 3.0:
            continue
        # Consolidation: next 3 bars range < 0.5%
        consol = bars.iloc[i:i + 3]
        consol_high = float(consol["high"].max())
        consol_low = float(consol["low"].min())
        if consol_low <= 0 or (consol_high - consol_low) / consol_low >= 0.005:
            continue
        # Break: last close above consolidation top
        if i + 3 < n and float(closes[-1]) > consol_high:
            return True, "first_pullback_bull_flag", consol_high, consol_low
    return False, "first_pullback_bull_flag", 0.0, None


def detect_flat_top_breakout(
    bars: pd.DataFrame,
) -> Tuple[bool, str, float, Optional[float]]:
    if bars.empty or len(bars) < 5:
        return False, "flat_top_breakout", 0.0, None
    recent = bars.tail(10)
    current_price = float(recent["close"].iloc[-1])
    # Price-relative tolerance: 0.3% of mid-range, minimum $0.03
    tolerance = max(0.03, current_price * 0.003)
    highs = recent["high"].values
    # Find the most common resistance level (cluster of equal highs)
    for resistance in highs:
        cluster = [h for h in highs if abs(h - resistance) <= tolerance]
        if len(cluster) >= 2 and current_price > resistance + tolerance:
            return True, "flat_top_breakout", float(resistance), None
    return False, "flat_top_breakout", 0.0, None


def run_setup_detectors(
    bars: pd.DataFrame,
    pm_levels: Optional[PremkLevels],
    prev_close: float,
    session_open_time: Optional[datetime] = None,
) -> List[Tuple[bool, str, float, Optional[float]]]:
    if bars.empty or pm_levels is None:
        return []
    results = [
        detect_premarket_high_break(bars, pm_levels.premarket_high),
        detect_pm_flag_break(bars, pm_levels.flags),
        detect_pm_pivot_break(bars, pm_levels.pivots),
        detect_red_to_green(bars, prev_close),
        detect_1min_orb(bars, session_open_time),
        detect_5min_orb(bars, session_open_time),
        detect_first_pullback_bull_flag(bars),
        detect_flat_top_breakout(bars),
    ]
    return results


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
    setup_name: str = ""
    catalyst_headline: str = ""
    setup_stop_ref: Optional[float] = None


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
