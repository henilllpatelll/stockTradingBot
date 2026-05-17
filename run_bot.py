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
    EntrySignal, FilterResult, PatternContext, PremkLevels,
    PremarketCatalystStrategy, mark_premarket_levels, run_setup_detectors,
)
from trade_logger import TradeLogger, TradeRecord

_ET = pytz.timezone("America/New_York")

# Duck-type for argparse.Namespace when called programmatically
_AutoArgs = namedtuple(
    "_AutoArgs",
    ["no_trading_hours_gate", "no_catalyst_check", "summary", "skip_filters", "skip_entry_signal"],
    defaults=[False, False, False, False, False],
)


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


def _exit_sell(client: AlpacaClient, symbol: str, qty: int, log: logging.Logger) -> None:
    quote = client.get_quote_with_spread(symbol)
    if quote["wide_spread"]:
        log.warning("%s: wide spread %.1f%% at exit", symbol, quote["spread_pct"])
    log.info("%s: exit sell %d shares @ $%.2f (bid)", symbol, qty, quote["bid"])
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

def monitor_position(
    *, client: AlpacaClient, risk_mgr: RiskManager, trade_logger: TradeLogger,
    config: Config, trade: TradeRecord, sizing: PositionSizing,
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

    targets = risk_mgr.calculate_targets(sizing.entry_price, sizing.initial_stop)
    shares_remaining = sizing.shares
    t1_hit = False
    last_candle_minute = None
    entry_bar_volume: Optional[float] = None
    bars_since_entry: pd.DataFrame = pd.DataFrame()

    log.info("%s: T1=$%.2f (2:1, sell 50%%) → breakeven stop → red-candle/extension exit",
             symbol, targets.t1)

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
        log.info("%s  price=$%.2f  stop=$%.2f  unrealized=$%+.2f  shares=%d  elapsed=%.0fm",
                 symbol, current_price, current_stop,
                 float(position.unrealized_pl), shares_remaining, elapsed / 60)

        # ---- New completed 1-min candle check ----
        current_minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        if current_minute != last_candle_minute:
            try:
                bars = client.get_intraday_bars(symbol, lookback_hours=1)
                last_candle_minute = current_minute
                if not bars.empty:
                    if entry_bar_volume is None:
                        entry_bar_volume = float(bars["volume"].iloc[-1])
                    bars_since_entry = bars
                    atr = _compute_atr(bars)

                    if len(bars) >= 2:
                        completed = bars.iloc[-2]  # last COMPLETED candle (not in-progress)

                        if t1_hit:
                            # Red-candle exit
                            if risk_mgr.is_red_candle(completed):
                                log.info("%s: red-candle exit — selling %d shares", symbol, shares_remaining)
                                _exit_sell(client, symbol, shares_remaining, log)
                                trade.shares = shares_remaining
                                _record_exit(trade=trade, exit_price=current_price,
                                             reason="red_candle_exit",
                                             trade_logger=trade_logger, client=client, symbol=symbol)
                                break

                            # Extension-bar exit
                            if atr and risk_mgr.is_extension_bar(completed, atr):
                                log.info("%s: extension-bar exit — selling %d shares", symbol, shares_remaining)
                                _exit_sell(client, symbol, shares_remaining, log)
                                trade.shares = shares_remaining
                                _record_exit(trade=trade, exit_price=current_price,
                                             reason="extension_bar_exit",
                                             trade_logger=trade_logger, client=client, symbol=symbol)
                                break

                            # ATR trailing stop raise
                            if atr:
                                candidate = round(float(bars["low"].iloc[-2]) - atr * config.risk.atr_multiplier, 4)
                                if current_stop < candidate < current_price:
                                    log.info("%s: stop raised $%.2f → $%.2f", symbol, current_stop, candidate)
                                    current_stop = candidate

                        else:
                            # Stall exit (before T1)
                            if risk_mgr.is_stall(bars_since_entry, sizing.entry_price, targets.t1):
                                log.info("%s: stall exit — no progress toward T1=$%.2f", symbol, targets.t1)
                                _exit_sell(client, symbol, shares_remaining, log)
                                trade.shares = shares_remaining
                                _record_exit(trade=trade, exit_price=current_price,
                                             reason="stall_exit",
                                             trade_logger=trade_logger, client=client, symbol=symbol)
                                break

                            # Momentum-fail exit (before T1)
                            if entry_bar_volume and risk_mgr.is_momentum_fail(bars, entry_bar_volume):
                                log.info("%s: momentum-fail exit — volume collapsed", symbol)
                                _exit_sell(client, symbol, shares_remaining, log)
                                trade.shares = shares_remaining
                                _record_exit(trade=trade, exit_price=current_price,
                                             reason="momentum_fail_exit",
                                             trade_logger=trade_logger, client=client, symbol=symbol)
                                break

            except Exception as exc:
                log.warning("%s: bar fetch failed: %s", symbol, exc)

        # ---- T1: sell 50% at 2:1, move stop to breakeven ----
        if not t1_hit and current_price >= targets.t1:
            t1_shares = shares_remaining // 2
            if t1_shares > 0:
                log.info("%s: T1 $%.2f — selling %d shares (50%%), stop → breakeven $%.2f",
                         symbol, targets.t1, t1_shares, sizing.entry_price)
                try:
                    _exit_sell(client, symbol, t1_shares, log)
                    shares_remaining -= t1_shares
                    current_stop = sizing.entry_price
                except Exception as exc:
                    log.error("%s: T1 sell failed: %s", symbol, exc)
            t1_hit = True

        # ---- Hard stop ----
        if risk_mgr.is_stop_hit(current_price, current_stop):
            reason = "breakeven_stop" if t1_hit else "stop_loss"
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
) -> None:
    log = logging.getLogger("run_bot")

    # Trading hours gate
    if not getattr(args, "no_trading_hours_gate", False) and not _is_trading_hours(config):
        log.warning("Outside trading window (%d–%d ET) — no trade for %s",
                    config.trading_start_hour_et, config.trading_end_hour_et, symbol)
        print(f"\n{symbol}: outside 7–11 AM ET trading window — no trade.\n")
        return

    trade_logger = TradeLogger(config.log_dir)
    client = AlpacaClient(config)
    strategy = PremarketCatalystStrategy(config, client)
    risk_mgr = RiskManager(config)

    account = client.get_account()
    equity = float(account.equity)
    log.info("Paper account  equity=$%.2f  buying_power=$%.2f", equity, float(account.buying_power))

    # ---- Premarket filters ----------------------------------------
    if not skip_filters:
        log.info("=== Running premarket filters for %s ===", symbol)
        filter_result = strategy.run_filters(symbol)
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
                log.info("%s: confirmed setups: %s", symbol, [s[0] for s in active_setups])
    except Exception as exc:
        log.warning("%s: setup detection failed: %s", symbol, exc)

    # Require at least 1 confirmed setup (unless testing)
    if not skip_entry_signal and not active_setups:
        print(f"\n{symbol}: no confirmed setup pattern — no trade placed.\n")
        trade_logger.print_summary()
        return

    # ---- AI catalyst evaluation ----------------------------------
    catalyst_headline = ""
    if not getattr(args, "no_catalyst_check", False) and config.news_evaluator.enabled:
        try:
            from news_evaluator import TradingAI
            ai = TradingAI(config.news_evaluator)
            headlines = client.get_news(symbol, lookback_hours=config.scanner.news_lookback_hours)
            catalyst_headline = headlines[0] if headlines else ""
            pattern_ctx = None
            if pm_levels and active_setups:
                mid = filter_result.price
                pattern_ctx = PatternContext(
                    setup_name=active_setups[0][0],
                    pm_high=pm_levels.premarket_high,
                    pm_low=pm_levels.premarket_low,
                    vwap=pm_levels.vwap,
                    current_price=mid,
                    price_vs_vwap_pct=(mid - pm_levels.vwap) / pm_levels.vwap * 100 if pm_levels.vwap else 0,
                    price_vs_pm_high_pct=(mid - pm_levels.premarket_high) / pm_levels.premarket_high * 100 if pm_levels.premarket_high else 0,
                    active_setups=[s[0] for s in active_setups],
                )
            decision = ai.assess(
                symbol=symbol, headlines=headlines,
                filter_data={"rvol": filter_result.rvol, "gap_pct": filter_result.change_pct,
                              "price": filter_result.price, "float_shares": filter_result.float_shares},
                setup_name=active_setups[0][0] if active_setups else "",
                pattern_ctx=pattern_ctx,
            )
            if not decision.should_trade:
                log.info("%s: AI rejected  confidence=%.0f%%  quality=%s",
                         symbol, decision.confidence * 100, decision.catalyst_quality)
                print(f"\n{symbol}: AI rejected (confidence {decision.confidence:.0%}, {decision.catalyst_quality}) — no trade.\n")
                return
            log.info("%s: AI approved  confidence=%.0f%%  setup=%s  catalyst=%s",
                     symbol, decision.confidence * 100, decision.setup_quality, decision.catalyst_type)
        except ImportError:
            log.warning("news_evaluator not available — skipping AI evaluation")
        except Exception as exc:
            log.warning("%s: AI evaluation failed (%s) — skipping", symbol, exc)

    # ---- Entry signal confirmation --------------------------------
    setup_stop_ref: Optional[float] = active_setups[0][2] if active_setups else None

    if not skip_entry_signal:
        signal = strategy.check_entry_signal(symbol, filter_result)
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

    # ---- Risk checks ---------------------------------------------
    if not risk_mgr.check_max_daily_loss(equity, equity):
        print("\nMax daily loss limit reached — no new trades.\n")
        return

    atr = None
    try:
        atr_bars = client.get_intraday_bars(symbol, lookback_hours=1)
        if not atr_bars.empty:
            atr = _compute_atr(atr_bars)
    except Exception:
        pass

    sizing = risk_mgr.calculate_position_size(equity, signal.entry_price, atr=atr, setup_stop_ref=signal.setup_stop_ref)
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
    )
    trade_logger.log_entry(trade)

    print(
        f"\n{'═' * 52}\n  PAPER TRADE ENTERED: {symbol}\n{'═' * 52}\n"
        f"  Entry : ${signal.entry_price:.2f}  ×{sizing.shares} shares  = ${sizing.position_value:.2f}\n"
        f"  Stop  : ${sizing.initial_stop:.2f}   Risk = ${sizing.risk_amount:.2f}\n"
        f"  T1    : ${risk_mgr.calculate_targets(sizing.entry_price, sizing.initial_stop).t1:.2f} (2:1)\n"
        f"  Setup : {signal.setup_name}\n{'═' * 52}\n"
    )

    # ---- Monitor position ----------------------------------------
    try:
        monitor_position(client=client, risk_mgr=risk_mgr, trade_logger=trade_logger,
                         config=config, trade=trade, sizing=sizing)
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
