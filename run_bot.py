from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz

from alpaca_client import AlpacaClient
from config import Config
from risk_manager import PositionSizing, RiskManager
from strategy import (
    EntrySignal, FilterResult, PremkLevels,
    PremarketCatalystStrategy, mark_premarket_levels, run_setup_detectors,
)
from trade_logger import PerformanceTracker, TradeLogger, TradeRecord

_ET = pytz.timezone("America/New_York")

# Duck-type for argparse.Namespace when called programmatically
_AutoArgs = namedtuple(
    "_AutoArgs",
    [
        "no_trading_hours_gate",
        "no_catalyst_check",
        "summary",
        "skip_filters",
        "skip_entry_signal",
        "catalyst_headline",
    ],
    defaults=[False, False, False, False, False, ""],
)

_LEARNING_MIN_SAMPLE = 5


def setup_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(Path(log_dir) / "bot.log"),
        ],
    )


def validate_ticker(raw: str) -> str:
    symbol = raw.strip().upper()
    if not symbol.isalpha() or not (1 <= len(symbol) <= 5):
        raise ValueError(f"Invalid ticker: {raw!r} — must be 1–5 letters")
    return symbol


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_atr(bars: pd.DataFrame) -> Optional[float]:
    if bars is None or len(bars) < 2:
        return None
    prev_close = bars["close"].shift(1)
    tr = pd.concat([
        bars["high"] - bars["low"],
        (bars["high"] - prev_close).abs(),
        (bars["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    val = float(tr.rolling(14, min_periods=1).mean().iloc[-1])
    return val if val > 0 else None


def _is_trading_hours(config: Config) -> bool:
    now_et = datetime.now(_ET)
    return config.trading_start_hour_et <= now_et.hour < config.trading_end_hour_et


def _rank_setups_by_learning(active_setups: list, perf_stats: dict) -> list:
    """Prefer confirmed setups with enough history and better live win rates."""
    if len(active_setups) < 2 or not perf_stats:
        return active_setups

    by_setup = perf_stats.get("by_setup", {})

    def _rank_key(indexed_setup):
        idx, setup = indexed_setup
        setup_name = setup[0]
        stats = by_setup.get(setup_name, {})
        trades = int(stats.get("trades", 0) or 0)
        if trades < _LEARNING_MIN_SAMPLE:
            return (1, 0.50, 0.0, -idx)
        win_rate = float(stats.get("win_rate", 0.0) or 0.0)
        avg_pnl = float(stats.get("avg_pnl", 0.0) or 0.0)
        if win_rate < 0.45:
            return (0, win_rate, avg_pnl, -idx)
        return (
            2,
            win_rate,
            avg_pnl,
            -idx,
        )

    ranked = sorted(enumerate(active_setups), key=_rank_key, reverse=True)
    return [setup for _, setup in ranked]


def _clamped_ratio(value: float, full_strength_value: float) -> float:
    if full_strength_value <= 0:
        return 0.0
    return max(0.0, min(value / full_strength_value, 1.0))


def _entry_signal_quality(signal: EntrySignal, config: Config) -> float:
    """
    Convert the non-AI entry signal into a confidence-like score.

    The score rewards the same things the strategy already filters for: RVOL,
    gap strength, and accelerating volume.
    """
    rvol_score = _clamped_ratio(signal.rvol, config.filters.min_rvol * 2)
    gap_score = _clamped_ratio(signal.change_pct, config.filters.min_change_pct * 3)
    volume_score = _clamped_ratio(signal.vol_acceleration, 3.0)
    return round((0.45 * rvol_score) + (0.25 * gap_score) + (0.30 * volume_score), 4)


def _learned_entry_gate(signal: EntrySignal, config: Config, perf_tracker) -> tuple[bool, float, float]:
    base_threshold = config.news_evaluator.setup_confidence_overrides.get(
        signal.setup_name,
        config.news_evaluator.confidence_threshold,
    )
    learned_threshold = perf_tracker.adjusted_threshold(signal.setup_name, base_threshold)
    quality = _entry_signal_quality(signal, config)
    if learned_threshold == base_threshold:
        return True, quality, learned_threshold
    return quality >= learned_threshold, quality, learned_threshold


def _resolve_news_catalyst(client: AlpacaClient, symbol: str, config: Config, args=None) -> tuple[str, str]:
    headline_override = (getattr(args, "catalyst_headline", "") or "").strip()
    if headline_override:
        return headline_override, "news"

    try:
        headlines = client.get_news(symbol, lookback_hours=config.scanner.news_lookback_hours)
    except Exception:
        return "", ""

    if not headlines:
        return "", ""
    return str(headlines[0]), "news"


def _exit_sell(client: AlpacaClient, symbol: str, qty: int, log: logging.Logger) -> None:
    quote = client.get_quote_with_spread(symbol)
    if quote["wide_spread"]:
        log.warning("%s: wide spread %.1f%% at exit", symbol, quote["spread_pct"])
    log.info("%s: exit sell %d shares @ $%.2f (bid - $0.10)", symbol, qty, quote["bid"] - 0.10)
    client.place_exit_limit_order(symbol, qty, quote["bid"])


def _record_exit(
    *, trade: TradeRecord, exit_price: float, reason: str,
    trade_logger: TradeLogger, client: AlpacaClient, symbol: str,
) -> None:
    try:
        quote = client.get_latest_quote(symbol)
        exit_price = quote["mid"]
    except Exception:
        pass
    pnl = (exit_price - trade.entry_price) * trade.shares
    pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100
    trade.exit_time = _now_iso()
    trade.exit_price = round(exit_price, 4)
    trade.exit_reason = reason
    trade.pnl = round(pnl, 2)
    trade.pnl_pct = round(pnl_pct, 2)
    trade_logger.log_exit(trade)


# ------------------------------------------------------------------ #
# Position monitor loop                                               #
# ------------------------------------------------------------------ #

def _trailing_stop_pct(gain_pct: float) -> Optional[float]:
    """Trail percentage based on current gain level; None = no trail yet."""
    if gain_pct >= 0.60:
        return 0.02
    if gain_pct >= 0.40:
        return 0.03
    if gain_pct >= 0.20:
        return 0.05
    return None


def monitor_position(
    *, client: AlpacaClient, risk_mgr: RiskManager, trade_logger: TradeLogger,
    config: Config, trade: TradeRecord, sizing: PositionSizing,
    vwap: Optional[float] = None,
) -> None:
    log = logging.getLogger("monitor")
    symbol = trade.symbol
    current_stop = sizing.initial_stop
    start = time.monotonic()
    interval = config.monitor_interval_seconds
    max_secs = config.max_hold_minutes * 60

    log.info("Monitoring %s | entry=$%.2f  stop=$%.2f  max_hold=%dm",
             symbol, sizing.entry_price, current_stop, config.max_hold_minutes)

    _confirm_deadline = time.monotonic() + 15
    while client.get_position(symbol) is None:
        if time.monotonic() > _confirm_deadline:
            log.error("%s: position never appeared — aborting monitor", symbol)
            return
        time.sleep(1)

    shares_remaining = sizing.shares
    breakeven_hit = False
    r1_hit = False
    exhaustion_hit = False
    last_candle_minute = None
    entry_bar_volume: Optional[float] = None

    log.info(
        "%s: breakeven=+%.0f%%  R1=+%.0f%%(or whole$) sell %.0f%%  exhaustion sell %.0f%%  "
        "trail 20%%→5%% / 40%%→3%% / 60%%→2%%",
        symbol,
        config.risk.breakeven_gain_pct * 100,
        config.risk.r1_gain_pct * 100,
        config.risk.r1_sell_pct * 100,
        config.risk.exhaustion_sell_pct * 100,
    )

    while True:
        elapsed = time.monotonic() - start
        position = client.get_position(symbol)

        if position is None:
            log.info("%s: position closed externally", symbol)
            trade.shares = shares_remaining
            _record_exit(trade=trade, exit_price=sizing.entry_price,
                         reason="position_closed_externally",
                         trade_logger=trade_logger, client=client, symbol=symbol)
            break

        current_price = float(position.current_price)
        gain_pct = (current_price - sizing.entry_price) / sizing.entry_price

        log.info("%s  price=$%.2f  stop=$%.2f  gain=%+.1f%%  shares=%d  elapsed=%.0fm",
                 symbol, current_price, current_stop,
                 gain_pct * 100, shares_remaining, elapsed / 60)

        # ---- Breakeven: stop → entry at +10% (no sell) ----
        if not breakeven_hit and gain_pct >= config.risk.breakeven_gain_pct:
            current_stop = sizing.entry_price
            breakeven_hit = True
            log.info("%s: +%.0f%% — stop moved to breakeven $%.2f",
                     symbol, config.risk.breakeven_gain_pct * 100, current_stop)

        # ---- Trailing stop (active after R1 is hit) ----
        if r1_hit:
            trail_pct = _trailing_stop_pct(gain_pct)
            if trail_pct is not None:
                trail_stop = round(current_price * (1 - trail_pct), 4)
                if trail_stop > current_stop:
                    current_stop = trail_stop
                    log.info("%s: trailing stop -> $%.2f (%.0f%% trail at %+.1f%% gain)",
                             symbol, current_stop, trail_pct * 100, gain_pct * 100)

        # ---- Hard stop ----
        if risk_mgr.is_stop_hit(current_price, current_stop):
            reason = "breakeven_stop" if breakeven_hit else "stop_loss"
            log.warning("%s: %s price=$%.2f stop=$%.2f — closing %d shares",
                        symbol, reason, current_price, current_stop, shares_remaining)
            try:
                _exit_sell(client, symbol, shares_remaining, log)
            except Exception:
                pass
            trade.shares = shares_remaining
            _record_exit(trade=trade, exit_price=current_price, reason=reason,
                         trade_logger=trade_logger, client=client, symbol=symbol)
            break

        # ---- R1: sell 25% at first obvious resistance (every tick) ----
        if not r1_hit and (
            risk_mgr.is_near_whole_half_dollar(current_price)
            or gain_pct >= config.risk.r1_gain_pct
        ):
            r1_shares = max(1, int(sizing.shares * config.risk.r1_sell_pct))
            log.info("%s: R1 $%.2f — selling %d shares (%.0f%% of original)",
                     symbol, current_price, r1_shares, config.risk.r1_sell_pct * 100)
            try:
                _exit_sell(client, symbol, r1_shares, log)
                shares_remaining -= r1_shares
            except Exception as exc:
                log.error("%s: R1 sell failed: %s", symbol, exc)
            r1_hit = True

        # ---- Per-candle checks (exhaustion + big red exit) ----
        current_minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        if current_minute != last_candle_minute:
            try:
                bars = client.get_intraday_bars(symbol, lookback_hours=1)
                last_candle_minute = current_minute
                if not bars.empty:
                    if entry_bar_volume is None:
                        entry_bar_volume = float(bars["volume"].iloc[-1])
                    atr = _compute_atr(bars)

                    if len(bars) >= 2:
                        completed = bars.iloc[-2]

                        # Exhaustion: sell 50% of remaining on first extension bar or volume climax
                        if r1_hit and not exhaustion_hit:
                            is_ext = bool(atr and risk_mgr.is_extension_bar(completed, atr))
                            is_climax = bool(
                                entry_bar_volume
                                and risk_mgr.is_volume_climax(completed, entry_bar_volume)
                            )
                            if is_ext or is_climax:
                                exh_reason = "extension_bar" if is_ext else "volume_climax"
                                exh_shares = max(1, int(shares_remaining * config.risk.exhaustion_sell_pct))
                                log.info(
                                    "%s: exhaustion (%s) — selling %d shares (%.0f%% remaining)",
                                    symbol, exh_reason, exh_shares,
                                    config.risk.exhaustion_sell_pct * 100,
                                )
                                try:
                                    _exit_sell(client, symbol, exh_shares, log)
                                    shares_remaining -= exh_shares
                                except Exception as exc:
                                    log.error("%s: exhaustion sell failed: %s", symbol, exc)
                                exhaustion_hit = True

                        # Big red candle closing near its low → exit remainder
                        if exhaustion_hit and risk_mgr.is_big_red_near_low(completed, atr):
                            log.info("%s: big red near low — exiting %d remaining shares",
                                     symbol, shares_remaining)
                            _exit_sell(client, symbol, shares_remaining, log)
                            trade.shares = shares_remaining
                            _record_exit(trade=trade, exit_price=current_price,
                                         reason="big_red_near_low",
                                         trade_logger=trade_logger, client=client, symbol=symbol)
                            break

            except Exception as exc:
                log.warning("%s: bar fetch failed: %s", symbol, exc)

        # ---- Max hold time ----
        if elapsed >= max_secs:
            log.info("%s: max hold %dm — closing %d shares", symbol, config.max_hold_minutes, shares_remaining)
            try:
                client.cancel_orders_for_symbol(symbol)
                _exit_sell(client, symbol, shares_remaining, log)
            except Exception:
                pass
            trade.shares = shares_remaining
            _record_exit(trade=trade, exit_price=current_price, reason="max_hold_time",
                         trade_logger=trade_logger, client=client, symbol=symbol)
            break

        time.sleep(interval)


# ------------------------------------------------------------------ #
# Core execution flow                                                 #
# ------------------------------------------------------------------ #

def run(
    symbol: str,
    config: Config,
    skip_filters: bool = False,
    skip_entry_signal: bool = False,
    args=None,
    strategy: str = "catalyst",
) -> None:
    log = logging.getLogger("run_bot")

    # Trading hours gate
    if not getattr(args, "no_trading_hours_gate", False) and not _is_trading_hours(config):
        log.warning("Outside trading window (%d–%d ET) — no trade for %s",
                    config.trading_start_hour_et, config.trading_end_hour_et, symbol)
        print(f"\n{symbol}: outside 7–11 AM ET trading window — no trade.\n")
        return

    trade_logger = TradeLogger(config.log_dir, strategy=strategy)
    perf_tracker = PerformanceTracker(config.log_dir, strategy=strategy)
    perf_stats = perf_tracker.get_stats()
    client = AlpacaClient(config)
    strategy_impl = PremarketCatalystStrategy(config, client)
    risk_mgr = RiskManager(config)

    account = client.get_account()
    equity = float(account.equity)
    starting_equity = float(account.last_equity) if account.last_equity else equity
    log.info("Paper account  equity=$%.2f  buying_power=$%.2f", equity, float(account.buying_power))

    # ---- Premarket filters ----------------------------------------
    if not skip_filters:
        log.info("=== Running premarket filters for %s ===", symbol)
        filter_result = strategy_impl.run_filters(symbol)
        print(f"\n[{symbol}] Filter results:\n{filter_result.summary()}")
        if not filter_result.passed:
            print(f"\n{symbol} did not pass all filters — no trade placed.\n")
            trade_logger.print_summary()
            return
        log.info("%s passed all filters.", symbol)
    else:
        log.warning("=== Skipping premarket filters for %s ===", symbol)
        filter_result = FilterResult(passed=True)
        try:
            quote = client.get_latest_quote(symbol)
            filter_result.price = quote["mid"]
        except Exception as exc:
            log.error("Could not fetch quote for %s: %s", symbol, exc)
            return

    # ---- Wide-spread gate ----------------------------------------
    try:
        spread_info = client.get_quote_with_spread(symbol)
        if spread_info["wide_spread"]:
            log.warning("%s: spread %.1f%% > 2%% — skipping", symbol, spread_info["spread_pct"])
            print(f"\n{symbol}: spread {spread_info['spread_pct']:.1f}% too wide — no trade.\n")
            return
    except Exception as exc:
        log.warning("%s: spread check failed: %s", symbol, exc)

    # ---- Premarket levels + setup detection ----------------------
    pm_levels: Optional[PremkLevels] = None
    active_setups: list = []
    try:
        pm_bars = client.get_premarket_bars(symbol)
        if not pm_bars.empty:
            pm_levels = mark_premarket_levels(pm_bars)
            if pm_levels:
                log.info("%s: pm_high=$%.2f  pm_low=$%.2f  vwap=$%.2f  pivots=%d  flags=%d",
                         symbol, pm_levels.premarket_high, pm_levels.premarket_low,
                         pm_levels.vwap, len(pm_levels.pivots), len(pm_levels.flags))
    except Exception as exc:
        log.warning("%s: premarket levels failed: %s", symbol, exc)

    try:
        live_bars = client.get_intraday_bars(symbol, lookback_hours=2)
        if not live_bars.empty and pm_levels:
            now_et = datetime.now(_ET)
            session_open = _ET.localize(
                datetime(now_et.year, now_et.month, now_et.day, 9, 30, 0)
            ).astimezone(timezone.utc)
            setup_results = run_setup_detectors(live_bars, pm_levels, filter_result.prev_close, session_open)
            active_setups = [(name, trig, stop_ref) for ok, name, trig, stop_ref in setup_results if ok]
            if active_setups:
                ranked_setups = _rank_setups_by_learning(active_setups, perf_stats)
                if ranked_setups[0][0] != active_setups[0][0]:
                    log.info(
                        "%s: learned setup selection preferred %s over %s",
                        symbol,
                        ranked_setups[0][0],
                        active_setups[0][0],
                    )
                active_setups = ranked_setups
                log.info("%s: confirmed setups: %s", symbol, [s[0] for s in active_setups])
    except Exception as exc:
        log.warning("%s: setup detection failed: %s", symbol, exc)

    # Require at least 1 confirmed setup (unless testing)
    if not skip_entry_signal and not active_setups:
        print(f"\n{symbol}: no confirmed setup pattern — no trade placed.\n")
        trade_logger.print_summary()
        return

    # ---- News catalyst confirmation ------------------------------
    catalyst_headline = ""
    catalyst_type = ""
    if not getattr(args, "no_catalyst_check", False) and strategy == "catalyst":
        catalyst_headline, catalyst_type = _resolve_news_catalyst(client, symbol, config, args)
        if not catalyst_headline:
            print(f"\n{symbol}: no recent news catalyst found - no trade.\n")
            trade_logger.print_summary()
            return
        log.info("%s: news catalyst confirmed - trading headline: %s", symbol, catalyst_headline)
    else:
        catalyst_headline = (getattr(args, "catalyst_headline", "") or "").strip()
        catalyst_type = "news" if catalyst_headline else ""

    # ---- Entry signal confirmation --------------------------------
    setup_stop_ref: Optional[float] = active_setups[0][2] if active_setups else None

    if not skip_entry_signal:
        signal = strategy_impl.check_entry_signal(symbol, filter_result)
        if signal is None:
            print(f"\n{symbol}: entry signal not confirmed — no trade.\n")
            trade_logger.print_summary()
            return
        signal.setup_name = active_setups[0][0] if active_setups else ""
        signal.setup_stop_ref = setup_stop_ref
        signal.catalyst_headline = catalyst_headline
    else:
        log.warning("--skip-entry-signal: bypassing momentum check")
        quote = client.get_latest_quote(symbol)
        signal = EntrySignal(
            symbol=symbol, entry_price=quote["mid"],
            reason="skip_entry_signal_flag",
            rvol=filter_result.rvol, change_pct=filter_result.change_pct,
            vol_acceleration=0.0,
            setup_name=active_setups[0][0] if active_setups else "manual",
            catalyst_headline=catalyst_headline, setup_stop_ref=setup_stop_ref,
        )

    log.info("%s: entry confirmed — setup=%s  reason=%s", symbol, signal.setup_name, signal.reason)

    if not skip_entry_signal:
        learned_ok, signal_quality, learned_threshold = _learned_entry_gate(signal, config, perf_tracker)
        log.info(
            "%s: learned entry gate setup=%s quality=%.0f%% threshold=%.0f%% pass=%s",
            symbol,
            signal.setup_name,
            signal_quality * 100,
            learned_threshold * 100,
            learned_ok,
        )
        if not learned_ok:
            print(
                f"\n{symbol}: learned entry gate rejected {signal.setup_name} "
                f"(quality {signal_quality:.0%} below learned threshold {learned_threshold:.0%}) - no trade.\n"
            )
            trade_logger.print_summary()
            return

    # ---- Risk checks ---------------------------------------------
    if not risk_mgr.check_max_daily_loss(starting_equity, equity):
        print("\nMax daily loss limit reached — no new trades.\n")
        return

    # Fetch entry candle low — Warrior stop = low of the entry bar
    entry_bar_low = None
    try:
        entry_bars = client.get_intraday_bars(symbol, lookback_hours=1)
        if not entry_bars.empty:
            entry_bar_low = float(entry_bars["low"].iloc[-1])
    except Exception:
        pass

    sizing = risk_mgr.calculate_position_size(
        equity, signal.entry_price,
        entry_bar_low=entry_bar_low,
        setup_stop_ref=signal.setup_stop_ref,
    )
    log.info("Sizing  shares=%d  entry=$%.2f  stop=$%.2f  risk=$%.2f  value=$%.2f",
             sizing.shares, sizing.entry_price, sizing.initial_stop, sizing.risk_amount, sizing.position_value)

    if sizing.shares < 1:
        print("\nPosition size = 0 — account too small. No trade.\n")
        return

    # ---- Execute entry -------------------------------------------
    entry_quote = client.get_latest_quote(symbol)
    entry_order = client.place_entry_limit_order(symbol, sizing.shares, entry_quote["ask"])
    log.info("Entry order submitted: id=%s", entry_order.id)

    fill_timeout = config.risk.entry_fill_timeout_seconds
    fill_deadline = time.monotonic() + fill_timeout
    while True:
        order = client.get_order(entry_order.id)
        if order and order.status.value == "filled":
            log.info("Entry filled: id=%s", entry_order.id)
            break
        if time.monotonic() > fill_deadline:
            log.warning("%s: entry did not fill in %ds — cancelling", symbol, fill_timeout)
            client.cancel_order(entry_order.id)
            return
        time.sleep(1)

    # ---- Record entry --------------------------------------------
    trade = TradeRecord(
        symbol=symbol, entry_time=_now_iso(), entry_price=signal.entry_price,
        shares=sizing.shares, initial_stop=sizing.initial_stop, trail_pct=0.0,
        rvol=signal.rvol, change_pct=signal.change_pct,
        setup_name=signal.setup_name, catalyst_headline=signal.catalyst_headline,
        catalyst_type=catalyst_type, strategy=strategy,
    )
    trade_logger.log_entry(trade)

    _r1 = round(signal.entry_price * (1 + config.risk.r1_gain_pct), 2)
    _be = round(signal.entry_price * (1 + config.risk.breakeven_gain_pct), 2)
    print(
        f"\n{'=' * 52}\n  PAPER TRADE ENTERED: {symbol}\n{'=' * 52}\n"
        f"  Entry      : ${signal.entry_price:.2f}  x{sizing.shares} shares  = ${sizing.position_value:.2f}\n"
        f"  Stop       : ${sizing.initial_stop:.2f}   Risk = ${sizing.risk_amount:.2f}\n"
        f"  Breakeven  : ${_be:.2f} (+{config.risk.breakeven_gain_pct:.0%}) → stop to entry, no sell\n"
        f"  R1         : ${_r1:.2f} (+{config.risk.r1_gain_pct:.0%} or whole/$) → sell {config.risk.r1_sell_pct:.0%}\n"
        f"  Exhaustion : sell {config.risk.exhaustion_sell_pct:.0%} remaining on ext-bar / vol-climax\n"
        f"  Remainder  : big red near low OR 20→5%% / 40→3%% / 60→2%% trailing stop\n"
        f"  Setup      : {signal.setup_name}\n{'=' * 52}\n"
    )

    # ---- Monitor position ----------------------------------------
    try:
        monitor_position(client=client, risk_mgr=risk_mgr, trade_logger=trade_logger,
                         config=config, trade=trade, sizing=sizing,
                         vwap=pm_levels.vwap if pm_levels else None)
    except KeyboardInterrupt:
        log.info("Interrupted — closing %s", symbol)
        try:
            client.cancel_orders_for_symbol(symbol)
            _exit_sell(client, symbol, trade.shares, log)
            _record_exit(trade=trade, exit_price=signal.entry_price, reason="manual_interrupt",
                         trade_logger=trade_logger, client=client, symbol=symbol)
        except Exception as exc:
            log.error("Error closing on interrupt: %s", exc)

    trade_logger.print_summary()


# ------------------------------------------------------------------ #
# CLI                                                                 #
# ------------------------------------------------------------------ #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Warrior Trading autonomous paper-trading bot.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("ticker", nargs="?", type=str, help="Ticker symbol, e.g. AAPL")
    parser.add_argument("--skip-filters", action="store_true")
    parser.add_argument("--skip-entry-signal", action="store_true")
    parser.add_argument("--summary", action="store_true", help="Print trade summary and exit")
    parser.add_argument("--no-trading-hours-gate", action="store_true",
                        help="Allow trade outside 7-11 AM ET (testing only)")
    parser.add_argument("--no-catalyst-check", action="store_true",
                        help="Skip AI catalyst evaluation (testing only)")

    args = parser.parse_args()
    config = Config()
    setup_logging(config.log_dir)

    if args.summary:
        TradeLogger(config.log_dir).print_summary()
        return

    if not args.ticker:
        parser.print_help()
        sys.exit(1)

    try:
        symbol = validate_ticker(args.ticker)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        run(symbol, config, skip_filters=args.skip_filters,
            skip_entry_signal=args.skip_entry_signal, args=args)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.getLogger("run_bot").exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
