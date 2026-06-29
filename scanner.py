from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
import yfinance as yf

from edgar import EdgarSummary, summarize as _edgar_summarize

logger = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")


def _et_now() -> datetime:
    return datetime.now(_ET)


def is_scanner_hours(cfg) -> bool:
    """True between 4:00 AM ET and 9:30 AM ET."""
    now = _et_now()
    start = now.replace(
        hour=cfg.scanner.scan_start_hour_et, minute=0, second=0, microsecond=0
    )
    end = now.replace(
        hour=cfg.scanner.scan_end_hour_et,
        minute=cfg.scanner.scan_end_minute_et,
        second=0,
        microsecond=0,
    )
    return start <= now < end


def is_trading_hours(cfg) -> bool:
    """True between cfg.trading_start_hour_et and cfg.trading_end_hour_et ET.
    7:00 AM - 11:00 AM ET = 6:00 AM - 10:00 AM CT."""
    now = _et_now()
    start = now.replace(
        hour=cfg.trading_start_hour_et, minute=0, second=0, microsecond=0
    )
    end = now.replace(
        hour=cfg.trading_end_hour_et, minute=0, second=0, microsecond=0
    )
    return start <= now < end


def is_alert_window(cfg) -> tuple:
    """(True, hour) if within 1 minute of a 7, 8, or 9 AM ET alert window. Else (False, -1).
    These windows align with major news release times."""
    now = _et_now()
    for hour in cfg.scanner.alert_hours_et:
        window_start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        delta = (now - window_start).total_seconds()
        if 0 <= delta < 60:
            return (True, hour)
    return (False, -1)


def seconds_until_scanner_open(cfg) -> float:
    """Seconds until 4:00 AM ET -- today if not yet reached, tomorrow if already past."""
    now = _et_now()
    target = now.replace(
        hour=cfg.scanner.scan_start_hour_et, minute=0, second=0, microsecond=0
    )
    if now >= target:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()


@dataclass
class GapperCandidate:
    symbol: str
    price: float
    prev_close: float
    gap_pct: float
    rvol: float
    float_shares: float
    market_cap: float
    headline: str = ""
    catalyst_confirmed: bool = False
    scan_time: str = ""


class CatalystChecker:
    def __init__(self, config, client) -> None:
        # config = ScannerConfig, client = AlpacaClient
        self._cfg = config
        self._client = client
        self._last_edgar: Optional[EdgarSummary] = None

    def fetch_headlines(self, symbol: str, lookback_hours: int) -> list:
        """Calls self._client.get_news(). Returns [] on any error -- never raises.
        Rule: if you cannot explain why it is moving, don't trade."""
        try:
            return self._client.get_news(symbol, lookback_hours)
        except Exception:
            return []

    def _fetch_edgar(self, symbol: str) -> EdgarSummary:
        """Run full EDGAR check (8-K + Form 4 + 13F). Never raises."""
        try:
            return _edgar_summarize(symbol)
        except Exception:
            return EdgarSummary(symbol=symbol)

    def has_catalyst(self, symbol: str, headlines: list) -> tuple:
        """
        Returns (True, headline) if a valid catalyst is confirmed, (False, "") otherwise.

        Check order:
        1. EDGAR 8-K — block immediately on dilution risk (items 3.01/3.02).
        2. Alpaca news headlines — use first headline as catalyst.
        3. EDGAR positive 8-K fallback — use 8-K description when Alpaca returns nothing.
        4. No catalyst found — log and skip.

        Stores the EdgarSummary in self._last_edgar for confirm_with_user() to display.
        """
        edgar = self._fetch_edgar(symbol)
        self._last_edgar = edgar

        if edgar.has_dilution_risk:
            first = edgar.summary_lines[0] if edgar.summary_lines else "dilution filing detected"
            logger.warning("%s: EDGAR dilution risk -- skip | %s", symbol, first)
            return (False, "")

        if headlines:
            if edgar.summary_lines:
                logger.info("%s: EDGAR context: %s", symbol, edgar.summary_lines[0])
            return (True, headlines[0])

        if edgar.has_positive_catalyst and edgar.summary_lines:
            headline = edgar.summary_lines[0]
            logger.info("%s: no Alpaca news -- EDGAR 8-K catalyst: %s", symbol, headline)
            return (True, headline)

        logger.info("no catalyst: %s -- skip", symbol)
        return (False, "")

    def confirm_with_user(self, symbol: str, headline: str) -> bool:
        """Prints catalyst headline and EDGAR context, then asks for y/n confirmation."""
        print(f'\n[CATALYST] {symbol}: "{headline}"')
        edgar = self._last_edgar
        if edgar and edgar.summary_lines:
            print("  [EDGAR]")
            for line in edgar.summary_lines[:6]:
                print(f"    {line}")
            if edgar.has_dilution_risk:
                print("    *** DILUTION RISK — strongly consider skipping ***")
        answer = input("Confirm catalyst? [y/n]: ").strip().lower()
        return answer in ("y", "yes")


class PremarketScanner:
    def __init__(self, config, client) -> None:
        # config = full Config object
        self._config = config
        self._client = client
        self._fundamental_cache: dict = {}  # symbol -> (datetime, dict)
        self._seen_symbols: set = set()

    def _fetch_yf_fundamentals(self, symbol: str) -> dict:
        """Fetch float and market cap from yfinance. Cached — these don't change intraday."""
        try:
            info = yf.Ticker(symbol).info
            return {
                "float_shares": float(info.get("floatShares") or 0),
                "market_cap": float(info.get("marketCap") or 0),
            }
        except Exception as exc:
            logger.warning("yfinance failed for %s: %s", symbol, exc)
            return {"float_shares": 0, "market_cap": 0}

    def _get_fundamentals(self, symbol: str) -> dict:
        """Cached (30 min): float, market cap from yfinance + 30-day avg volume from Alpaca.
        Live each call: price, prev close, premarket volume from Alpaca SIP snapshot."""
        now = datetime.now(timezone.utc)
        cached = self._fundamental_cache.get(symbol)
        if cached and (now - cached[0]).total_seconds() < 1800:
            fund = cached[1]
        else:
            yf_data = self._fetch_yf_fundamentals(symbol)
            try:
                bars = self._client.get_daily_bars(symbol, days=30)
                avg_volume = float(bars["volume"].mean()) if not bars.empty else 0.0
            except Exception as exc:
                logger.warning("%s: 30-day avg volume fetch failed: %s", symbol, exc)
                avg_volume = 0.0
            fund = {
                "float_shares": yf_data["float_shares"],
                "market_cap": yf_data["market_cap"],
                "avg_volume": avg_volume,
            }
            self._fundamental_cache[symbol] = (now, fund)

        try:
            snapshot = self._client.get_snapshot(symbol)
            prev_close = float(snapshot.previous_daily_bar.close) if snapshot and snapshot.previous_daily_bar else 0.0
            premarket_price = float(snapshot.latest_trade.price) if snapshot and snapshot.latest_trade else 0.0
            premarket_vol = float(snapshot.daily_bar.volume) if snapshot and snapshot.daily_bar else 0.0
        except Exception as exc:
            logger.warning("Alpaca snapshot failed for %s: %s", symbol, exc)
            prev_close = premarket_price = premarket_vol = 0.0

        return {
            "float_shares": fund["float_shares"],
            "market_cap": fund["market_cap"],
            "avg_volume": fund["avg_volume"],
            "premarket_price": premarket_price,
            "prev_close": prev_close,
            "premarket_volume": premarket_vol,
        }

    def _check_symbol(self, symbol: str, filters=None) -> "GapperCandidate | None":
        filters = filters or self._config.filters
        try:
            data = self._get_fundamentals(symbol)

            price = data["premarket_price"]
            prev_close = data["prev_close"]

            if not price or not prev_close or prev_close <= 0:
                logger.debug("%s: no price data from Alpaca snapshot", symbol)
                return None

            gap_pct = (price - prev_close) / prev_close * 100

            # Filter 1: price $1-$20
            if not (filters.min_price <= price <= filters.max_price):
                logger.info(
                    "%s: price $%.2f outside $%.2f-$%.2f range",
                    symbol, price, filters.min_price, filters.max_price,
                )
                return None

            float_shares = data["float_shares"]
            market_cap = data["market_cap"]
            avg_volume = data["avg_volume"]

            # Filter 2: float < 20M shares
            if float_shares <= 0 or float_shares >= filters.max_float_shares:
                logger.info(
                    "%s: float %.1fM >= %.1fM limit",
                    symbol, float_shares / 1e6, filters.max_float_shares / 1e6,
                )
                return None

            # Filter 3: market cap < max_market_cap
            if market_cap > 0 and market_cap > filters.max_market_cap:
                logger.info(
                    "%s: market cap $%.0fM >= $%.0fM limit",
                    symbol, market_cap / 1e6, filters.max_market_cap / 1e6,
                )
                return None

            # Filter 4: RVOL >= min_rvol (skip gate when premarket volume is unavailable)
            premarket_vol = data["premarket_volume"]
            rvol = (premarket_vol / avg_volume) if avg_volume > 0 else 0.0
            if premarket_vol > 0 and rvol < filters.min_rvol:
                logger.info(
                    "%s: RVOL %.2fx < %.1fx minimum", symbol, rvol, filters.min_rvol
                )
                return None

            # Filter 5: gap >= min_change_pct
            if gap_pct < filters.min_change_pct:
                logger.info(
                    "%s: change %.2f%% < %.1f%% minimum", symbol, gap_pct, filters.min_change_pct
                )
                return None

            return GapperCandidate(
                symbol=symbol,
                price=price,
                prev_close=prev_close,
                gap_pct=round(gap_pct, 2),
                rvol=round(rvol, 2),
                float_shares=float_shares,
                market_cap=market_cap,
                scan_time=_et_now().strftime("%H:%M:%S ET"),
            )

        except Exception as exc:
            logger.warning("%s: scan error: %s", symbol, exc)
            return None

    def _check_symbol_with_detail(self, symbol: str, filters=None) -> tuple:
        """
        Like _check_symbol but distinguishes *why* it failed.
        Returns (GapperCandidate | None, only_momentum_failed: bool).
        only_momentum_failed=True means price/float/market_cap all passed
        but gap% and/or RVOL did not — worth putting on a pending watchlist.
        """
        filters = filters or self._config.filters
        try:
            data = self._get_fundamentals(symbol)
            price = data["premarket_price"]
            prev_close = data["prev_close"]

            if not price or not prev_close or prev_close <= 0:
                logger.debug("%s: no price data from Alpaca snapshot", symbol)
                return None, False

            gap_pct = (price - prev_close) / prev_close * 100

            if not (filters.min_price <= price <= filters.max_price):
                logger.info(
                    "%s: price $%.2f outside $%.2f-$%.2f range",
                    symbol, price, filters.min_price, filters.max_price,
                )
                return None, False

            float_shares = data["float_shares"]
            market_cap = data["market_cap"]
            avg_volume = data["avg_volume"]

            if float_shares <= 0 or float_shares >= filters.max_float_shares:
                logger.info(
                    "%s: float %.1fM >= %.1fM limit",
                    symbol, float_shares / 1e6, filters.max_float_shares / 1e6,
                )
                return None, False

            if market_cap > 0 and market_cap > filters.max_market_cap:
                logger.info(
                    "%s: market cap $%.0fM >= $%.0fM limit",
                    symbol, market_cap / 1e6, filters.max_market_cap / 1e6,
                )
                return None, False

            # Static filters passed — now check momentum-only filters
            premarket_vol = data["premarket_volume"]
            rvol = (premarket_vol / avg_volume) if avg_volume > 0 else 0.0
            rvol_fail = premarket_vol > 0 and rvol < filters.min_rvol
            gap_fail = gap_pct < filters.min_change_pct

            if rvol_fail or gap_fail:
                if rvol_fail:
                    logger.info(
                        "%s: pending — RVOL %.2fx < %.1fx minimum", symbol, rvol, filters.min_rvol
                    )
                if gap_fail:
                    logger.info(
                        "%s: pending — gap %.2f%% < %.1f%% minimum", symbol, gap_pct, filters.min_change_pct
                    )
                return None, True

            return GapperCandidate(
                symbol=symbol,
                price=price,
                prev_close=prev_close,
                gap_pct=round(gap_pct, 2),
                rvol=round(rvol, 2),
                float_shares=float_shares,
                market_cap=market_cap,
                scan_time=_et_now().strftime("%H:%M:%S ET"),
            ), False

        except Exception as exc:
            logger.warning("%s: scan error: %s", symbol, exc)
            return None, False

    def recheck_momentum(self, symbol: str, filters=None) -> "GapperCandidate | None":
        """
        Recheck gap% and RVOL only for a symbol already confirmed to pass static filters.
        Returns GapperCandidate if momentum now qualifies, None otherwise.
        """
        filters = filters or self._config.filters
        try:
            data = self._get_fundamentals(symbol)
            price = data["premarket_price"]
            prev_close = data["prev_close"]

            if not price or not prev_close or prev_close <= 0:
                return None

            gap_pct = (price - prev_close) / prev_close * 100
            premarket_vol = data["premarket_volume"]
            avg_volume = data["avg_volume"]
            rvol = (premarket_vol / avg_volume) if avg_volume > 0 else 0.0

            rvol_ok = premarket_vol <= 0 or rvol >= filters.min_rvol
            gap_ok = gap_pct >= filters.min_change_pct

            logger.debug(
                "%s recheck: gap %.1f%%(need %.1f%%) rvol %.1fx(need %.1fx) -> %s",
                symbol, gap_pct, filters.min_change_pct, rvol, filters.min_rvol,
                "PASS" if (rvol_ok and gap_ok) else "wait",
            )

            if not (rvol_ok and gap_ok):
                return None

            return GapperCandidate(
                symbol=symbol,
                price=price,
                prev_close=prev_close,
                gap_pct=round(gap_pct, 2),
                rvol=round(rvol, 2),
                float_shares=data["float_shares"],
                market_cap=data["market_cap"],
                scan_time=_et_now().strftime("%H:%M:%S ET"),
            )
        except Exception as exc:
            logger.warning("%s: recheck error: %s", symbol, exc)
            return None

    def scan_once(self, universe: list, filters=None) -> list:
        """Check all tickers. Return passing GapperCandidates sorted by RVOL descending."""
        candidates = []
        for symbol in universe:
            candidate = self._check_symbol(symbol, filters=filters)
            if candidate is not None:
                candidates.append(candidate)
        candidates.sort(key=lambda c: c.rvol, reverse=True)
        logger.info(
            "Scanned %d symbols -> %d passed all filters", len(universe), len(candidates)
        )
        return candidates

    def scan_once_with_detail(self, universe: list, filters=None) -> tuple[list, list]:
        """Like scan_once but also returns symbols that pass static filters but not yet momentum."""
        candidates = []
        pending = []
        for symbol in universe:
            candidate, only_momentum_failed = self._check_symbol_with_detail(symbol, filters=filters)
            if candidate is not None:
                candidates.append(candidate)
            elif only_momentum_failed:
                pending.append(symbol)
        candidates.sort(key=lambda c: c.rvol, reverse=True)
        logger.info(
            "Scanned %d symbols -> %d passed, %d pending momentum",
            len(universe), len(candidates), len(pending),
        )
        return candidates, pending

    def print_ranked_candidates(self, candidates: list) -> None:
        """Console table (sorted RVOL desc): RANK SYMBOL PRICE GAP% RVOL FLOAT(M) CATALYST"""
        if not candidates:
            print("No candidates passing filters.")
            return
        header = (
            f"{'RANK':<6}{'SYMBOL':<8}{'PRICE':>8}{'GAP%':>9}{'RVOL':>8}{'FLOAT(M)':>10}  CATALYST"
        )
        sep = "─" * 72
        print(f"\n{sep}")
        print(header)
        print(sep)
        for i, c in enumerate(candidates, 1):
            float_m = c.float_shares / 1e6
            catalyst_str = c.headline[:30] if c.headline else ("YES" if c.catalyst_confirmed else "--")
            print(
                f"{i:<6}{c.symbol:<8}{c.price:>8.2f}"
                f"{c.gap_pct:>8.2f}%{c.rvol:>7.1f}x"
                f"{float_m:>9.1f}M  {catalyst_str}"
            )
        print(f"{sep}\n")

    def run_scan_loop(self, universe: list | None, catalyst_checker, on_candidate=None) -> None:
        """
        Outer loop:
        1. If outside scanner hours (before 4 AM ET), sleep until open.
        2. Every 30 seconds: fetch dynamic universe (or use fixed list), then scan_once().
        3. For each new candidate not in self._seen_symbols:
             - fetch_headlines() -> has_catalyst()
             - If no catalyst: log and skip (do not add to seen -- re-check next scan)
             - If catalyst: add to seen, print ranked table
        4. At 7, 8, 9 AM ET windows: print 'Running Up' alert for all active candidates.
        5. Stop loop at 9:30 AM ET.
        6. On Ctrl-C: print final ranked table and exit gracefully.
        Pass universe=None to auto-discover stocks via the Alpaca screener on every scan.
        Pass a list to scan a fixed set of tickers instead.
        Loop runs until scan_end_hour_et (default 8 PM ET).
        """
        cfg = self._config
        refresh = cfg.scanner.refresh_interval_seconds
        dynamic = universe is None

        if not is_scanner_hours(cfg):
            secs = seconds_until_scanner_open(cfg)
            if secs > 0:
                print(
                    f"Scanner opens at 4:00 AM ET -- sleeping {secs / 3600:.1f}h ({secs:.0f}s)..."
                )
                time.sleep(secs)

        mode_str = "dynamic" if dynamic else f"{len(universe)} symbols"
        print(f"[{_et_now().strftime('%H:%M:%S ET')}] Premarket scanner started. Mode: {mode_str}.")

        active_candidates: dict[str, GapperCandidate] = {}
        pending_symbols: set[str] = set()
        last_alert_hour = -1

        try:
            while is_scanner_hours(cfg):
                now_str = _et_now().strftime("%H:%M:%S ET")
                current_universe = (
                    self._client.get_market_universe() if dynamic else universe
                )
                if not current_universe:
                    logger.warning("Empty universe -- retrying in %ds", refresh)
                    time.sleep(refresh)
                    continue
                candidates, new_pending = self.scan_once_with_detail(current_universe)

                # Update pending watchlist
                candidate_symbols = {c.symbol for c in candidates}
                for sym in new_pending:
                    if sym not in self._seen_symbols and sym not in candidate_symbols:
                        if sym not in pending_symbols:
                            logger.info("%s: pending watchlist — passes static filters, awaiting gap/RVOL", sym)
                        pending_symbols.add(sym)
                pending_symbols -= candidate_symbols

                # Recheck pending symbols — promote any that now meet gap/RVOL
                for sym in list(pending_symbols):
                    promoted = self.recheck_momentum(sym)
                    if promoted is not None:
                        logger.info(
                            "%s: promoted from pending — gap=%.1f%% rvol=%.1fx",
                            sym, promoted.gap_pct, promoted.rvol,
                        )
                        pending_symbols.discard(sym)
                        candidates.append(promoted)
                candidates.sort(key=lambda c: c.rvol, reverse=True)

                no_catalyst = 0
                for c in candidates:
                    if c.symbol not in self._seen_symbols:
                        headlines = catalyst_checker.fetch_headlines(
                            c.symbol, cfg.scanner.news_lookback_hours
                        )
                        has_cat, headline = catalyst_checker.has_catalyst(c.symbol, headlines)
                        if not has_cat:
                            no_catalyst += 1
                            continue
                        c.headline = headline
                        c.catalyst_confirmed = True
                        self._seen_symbols.add(c.symbol)
                        active_candidates[c.symbol] = c
                        print(
                            f"\n[{now_str}] NEW GAPPER: {c.symbol}"
                            f"  +{c.gap_pct:.1f}%  RVOL {c.rvol:.1f}x"
                        )
                        self.print_ranked_candidates(
                            sorted(active_candidates.values(), key=lambda x: x.rvol, reverse=True)
                        )
                        if on_candidate:
                            on_candidate(c)
                    else:
                        active_candidates[c.symbol] = c

                # Remove watch-list entries that no longer pass filters
                passing = {c.symbol for c in candidates}
                for stale in [s for s in active_candidates if s not in passing]:
                    logger.info("%s: faded below filters -- removed from watch list", stale)
                    del active_candidates[stale]

                if no_catalyst or pending_symbols:
                    logger.info(
                        "%d passed filters -- %d skipped (no catalyst) -- %d pending momentum",
                        len(candidates), no_catalyst, len(pending_symbols),
                    )

                in_alert, alert_hour = is_alert_window(cfg)
                if in_alert and alert_hour != last_alert_hour and active_candidates:
                    last_alert_hour = alert_hour
                    print(
                        f"\n[{now_str}] === {alert_hour}:00 AM ET ALERT -- Running Up Candidates ==="
                    )
                    self.print_ranked_candidates(
                        sorted(active_candidates.values(), key=lambda x: x.rvol, reverse=True)
                    )

                time.sleep(refresh)

        except KeyboardInterrupt:
            print(f"\n[{_et_now().strftime('%H:%M:%S ET')}] Scanner stopped -- final candidates:")
            if active_candidates:
                self.print_ranked_candidates(
                    sorted(active_candidates.values(), key=lambda x: x.rvol, reverse=True)
                )
            return

        print(f"\n[{_et_now().strftime('%H:%M:%S ET')}] 9:30 AM ET -- scanner closed. Final candidates:")
        if active_candidates:
            self.print_ranked_candidates(
                sorted(active_candidates.values(), key=lambda x: x.rvol, reverse=True)
            )
