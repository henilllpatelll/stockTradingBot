"""
run_bot.py — CLI entry point for the premarket catalyst paper-trading bot.

Usage:
    python run_bot.py TICKER [options]

Examples:
    python run_bot.py BBAI
    python run_bot.py MSTR
    python run_bot.py --summary
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from alpaca.trading.enums import OrderSide

from alpaca_client import AlpacaClient
from config import Config
from risk_manager import PositionSizing, RiskManager, TakeProfitTargets
from strategy import EntrySignal, FilterResult, PremarketCatalystStrategy
from trade_logger import TradeLogger, TradeRecord


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

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


def _limit_sell(client: AlpacaClient, symbol: str, qty: int, log: logging.Logger) -> None:
    quote = client.get_latest_quote(symbol)
    limit = round(quote["bid"] - 0.05, 2)
    log.info("%s: limit sell %d shares @ $%.2f (bid-$0.05)", symbol, qty, limit)
    client.place_limit_order(symbol, qty, OrderSide.SELL, limit)


# ------------------------------------------------------------------ #
# Position monitor loop                                               #
# ------------------------------------------------------------------ #

def monitor_position(
    *,
    client: AlpacaClient,
    risk_mgr: RiskManager,
    trade_logger: TradeLogger,
    config: Config,
    trade: TradeRecord,
    sizing: PositionSizing,
) -> None:
    log = logging.getLogger("monitor")
    symbol = trade.symbol
    current_stop = sizing.initial_stop
    start = time.monotonic()
    interval = config.monitor_interval_seconds
    max_secs = config.max_hold_minutes * 60

    log.info(
        "Monitoring %s | entry=$%.2f  stop=$%.2f  trail=candle-low  max_hold=%dm",
        symbol,
        sizing.entry_price,
        current_stop,
        config.max_hold_minutes,
    )

    # Wait for the position to appear in Alpaca's API before polling.
    # Paper fills are near-instant but the positions endpoint can lag by a few seconds.
    _confirm_deadline = time.monotonic() + 15
    while client.get_position(symbol) is None:
        if time.monotonic() > _confirm_deadline:
            log.error("%s: position never appeared after entry — aborting monitor", symbol)
            return
        time.sleep(1)

    targets = risk_mgr.calculate_targets(sizing.entry_price, sizing.initial_stop)
    shares_remaining = sizing.shares
    t1_hit = False
    t2_hit = False
    last_bar_time = None
    last_candle_minute = None
    log.info(
        "%s: targets  T1=$%.2f (1:1, sell 50%%)  T2=$%.2f (before $%.2f psych, sell 25%%)"
        "  T3=candle closes below 9-EMA",
        symbol,
        targets.t1,
        targets.t2,
        targets.t2_psych,
    )

    while True:
        elapsed = time.monotonic() - start

        position = client.get_position(symbol)

        # Position closed externally — position no longer exists.
        if position is None:
            log.info("%s: position closed externally", symbol)
            trade.shares = shares_remaining
            _record_exit(
                trade=trade,
                exit_price=sizing.entry_price,  # rough fallback
                reason="position_closed_externally",
                trade_logger=trade_logger,
                client=client,
                symbol=symbol,
            )
            break

        current_price = float(position.current_price)
        unrealized = float(position.unrealized_pl)

        log.info(
            "%s  price=$%.2f  stop=$%.2f  unrealized=${:+.2f}  shares=%d  elapsed=%.0fm".format(
                unrealized
            ),
            symbol,
            current_price,
            current_stop,
            shares_remaining,
            elapsed / 60,
        )

        # ---- Fetch bars on each new completed candle (top of each minute) ----
        current_minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        if current_minute != last_candle_minute:
            try:
                bars = client.get_intraday_bars(symbol, lookback_hours=1)
                last_candle_minute = current_minute
                if len(bars) >= 2:
                    new_bar_time = bars.index[-1]
                    if new_bar_time != last_bar_time:
                        last_bar_time = new_bar_time
                        if t1_hit:
                            # ATR(14) from recent 1-min bars
                            prev_close = bars["close"].shift(1)
                            tr = pd.concat([
                                bars["high"] - bars["low"],
                                (bars["high"] - prev_close).abs(),
                                (bars["low"]  - prev_close).abs(),
                            ], axis=1).max(axis=1)
                            atr = float(tr.rolling(14, min_periods=1).mean().iloc[-1])
                            prev_candle_low = float(bars["low"].iloc[-1])
                            candidate = round(prev_candle_low - atr * config.risk.atr_multiplier, 4)
                            if candidate > current_stop:
                                log.info(
                                    "%s: stop raised  candle_low=$%.2f  atr=$%.4f  new_stop=$%.2f -> $%.2f",
                                    symbol, prev_candle_low, atr, current_stop, candidate,
                                )
                                current_stop = candidate

                    # T3: sell remaining if a candle closes below the 9-EMA
                    if t1_hit and t2_hit and len(bars) >= 9:
                        ema9 = float(bars["close"].ewm(span=9, adjust=False).mean().iloc[-1])
                        last_close = float(bars["close"].iloc[-1])
                        if last_close < ema9:
                            log.info(
                                "%s: T3 candle closed below 9-EMA ($%.2f < $%.2f) — selling remaining %d shares",
                                symbol, last_close, ema9, shares_remaining,
                            )
                            _limit_sell(client, symbol, shares_remaining, log)
                            trade.shares = shares_remaining
                            _record_exit(
                                trade=trade,
                                exit_price=current_price,
                                reason="take_profit_t3_ema_close",
                                trade_logger=trade_logger,
                                client=client,
                                symbol=symbol,
                            )
                            break
            except Exception as exc:
                log.warning("%s: bar fetch failed: %s", symbol, exc)

        # ---- Take-profit T1: sell 50% at 1:1 risk/reward ----
        if not t1_hit and current_price >= targets.t1:
            t1_shares = shares_remaining // 2
            if t1_shares > 0:
                log.info(
                    "%s: T1 hit  price=$%.2f >= $%.2f — selling %d shares (50%%)",
                    symbol, current_price, targets.t1, t1_shares,
                )
                try:
                    _limit_sell(client, symbol, t1_shares, log)
                    shares_remaining -= t1_shares
                except Exception as exc:
                    log.error("%s: T1 sell failed: %s", symbol, exc)
            t1_hit = True

        # ---- Take-profit T2: sell 50% of remainder just before psych level ----
        elif t1_hit and not t2_hit and current_price >= targets.t2:
            t2_shares = shares_remaining // 2
            if t2_shares > 0:
                log.info(
                    "%s: T2 hit  price=$%.2f >= $%.2f (before $%.2f) — selling %d shares",
                    symbol, current_price, targets.t2, targets.t2_psych, t2_shares,
                )
                try:
                    _limit_sell(client, symbol, t2_shares, log)
                    shares_remaining -= t2_shares
                except Exception as exc:
                    log.error("%s: T2 sell failed: %s", symbol, exc)
            t2_hit = True

        # ---- Hard stop hit — close remaining shares ----
        if risk_mgr.is_stop_hit(current_price, current_stop):
            log.warning(
                "%s: stop hit  price=$%.2f  stop=$%.2f — closing %d shares",
                symbol, current_price, current_stop, shares_remaining,
            )
            try:
                _limit_sell(client, symbol, shares_remaining, log)
            except Exception:
                pass
            trade.shares = shares_remaining
            _record_exit(
                trade=trade,
                exit_price=current_price,
                reason="stop_loss",
                trade_logger=trade_logger,
                client=client,
                symbol=symbol,
            )
            break

        # ---- Max hold time reached ----
        if elapsed >= max_secs:
            log.info(
                "%s: max hold time (%dm) reached — closing %d shares",
                symbol, config.max_hold_minutes, shares_remaining,
            )
            try:
                client.cancel_orders_for_symbol(symbol)
                _limit_sell(client, symbol, shares_remaining, log)
            except Exception:
                pass
            trade.shares = shares_remaining
            _record_exit(
                trade=trade,
                exit_price=current_price,
                reason="max_hold_time",
                trade_logger=trade_logger,
                client=client,
                symbol=symbol,
            )
            break

        time.sleep(interval)


def _record_exit(
    *,
    trade: TradeRecord,
    exit_price: float,
    reason: str,
    trade_logger: TradeLogger,
    client: AlpacaClient,
    symbol: str,
) -> None:
    # Try to get a better exit price from the live quote
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
# Core execution flow                                                 #
# ------------------------------------------------------------------ #

def run(
    symbol: str,
    config: Config,
    skip_filters: bool = False,
    skip_entry_signal: bool = False,
) -> None:
    log = logging.getLogger("run_bot")

    trade_logger = TradeLogger(config.log_dir)
    client = AlpacaClient(config)
    strategy = PremarketCatalystStrategy(config, client)
    risk_mgr = RiskManager(config)

    # Validate API connectivity and report account balance
    account = client.get_account()
    equity = float(account.equity)
    log.info("Paper account  equity=$%.2f  buying_power=$%.2f", equity, float(account.buying_power))

    # ---- Premarket filters ----------------------------------------
    filter_result: FilterResult
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
            log.error(
                "Could not fetch latest quote for %s while skipping filters: %s",
                symbol,
                exc,
            )
            print(f"\n{symbol}: could not fetch latest quote — no trade placed.\n")
            return

    # ---- Entry signal confirmation --------------------------------
    if not skip_entry_signal:
        log.info("Checking entry signal...")
        signal: EntrySignal | None = strategy.check_entry_signal(symbol, filter_result)
        if signal is None:
            print(
                f"\n{symbol} passed filters but entry signal not confirmed "
                "(insufficient volume acceleration or price strength). No trade placed.\n"
            )
            trade_logger.print_summary()
            return
    else:
        log.warning("--skip-entry-signal active — bypassing momentum check")
        # Build a minimal signal from filter data
        quote = client.get_latest_quote(symbol)
        signal = EntrySignal(
            symbol=symbol,
            entry_price=quote["mid"],
            reason="skip_entry_signal_flag",
            rvol=filter_result.rvol,
            change_pct=filter_result.change_pct,
            vol_acceleration=0.0,
        )

    log.info("%s: entry signal confirmed — %s", symbol, signal.reason)

    # ---- Risk checks ---------------------------------------------
    starting_equity = equity
    if not risk_mgr.check_max_daily_loss(starting_equity, equity):
        print("\nMax daily loss limit already reached — no new trades.\n")
        return

    sizing = risk_mgr.calculate_position_size(equity, signal.entry_price)
    log.info(
        "Position sizing  shares=%d  entry=$%.2f  stop=$%.2f  risk=$%.2f  value=$%.2f",
        sizing.shares,
        sizing.entry_price,
        sizing.initial_stop,
        sizing.risk_amount,
        sizing.position_value,
    )

    if sizing.shares < 1:
        print("\nPosition size rounds to 0 shares — account equity too small. No trade.\n")
        return

    # ---- Execute entry -------------------------------------------
    entry_quote = client.get_latest_quote(symbol)
    entry_limit = round(entry_quote["ask"] + 0.05, 2)
    entry_order = client.place_limit_order(symbol, sizing.shares, OrderSide.BUY, entry_limit)
    log.info("Entry limit order submitted: id=%s  limit=$%.2f", entry_order.id, entry_limit)

    fill_deadline = time.monotonic() + 30
    while True:
        order = client.get_order(entry_order.id)
        if order is not None and order.status.value == "filled":
            log.info("Entry order filled: id=%s", entry_order.id)
            break
        if time.monotonic() > fill_deadline:
            log.warning("%s: entry limit did not fill within 30s — cancelling", symbol)
            client.cancel_order(entry_order.id)
            return
        time.sleep(1)

    log.info(
        "Managing stops manually for %s — initial stop=$%.2f, trail=candle-low",
        symbol,
        sizing.initial_stop,
    )

    # ---- Record entry --------------------------------------------
    trade = TradeRecord(
        symbol=symbol,
        entry_time=_now_iso(),
        entry_price=signal.entry_price,
        shares=sizing.shares,
        initial_stop=sizing.initial_stop,
        trail_pct=0.0,
        rvol=signal.rvol,
        change_pct=signal.change_pct,
    )
    trade_logger.log_entry(trade)

    print(
        f"\n{'═' * 52}\n"
        f"  PAPER TRADE ENTERED: {symbol}\n"
        f"{'═' * 52}\n"
        f"  Entry price : ${signal.entry_price:.2f}\n"
        f"  Shares      : {sizing.shares}\n"
        f"  Position    : ${sizing.position_value:.2f}\n"
        f"  Stop loss   : ${sizing.initial_stop:.2f}  (-${config.risk.initial_stop_offset:.2f})\n"
        f"  Trail stop  : candle-low\n"
        f"  Max risk    : ${sizing.risk_amount:.2f}\n"
        f"  Signal      : {signal.reason}\n"
        f"{'═' * 52}\n"
    )

    # ---- Monitor position ----------------------------------------
    try:
        monitor_position(
            client=client,
            risk_mgr=risk_mgr,
            trade_logger=trade_logger,
            config=config,
            trade=trade,
            sizing=sizing,
        )
    except KeyboardInterrupt:
        log.info("Interrupted — closing %s", symbol)
        try:
            client.cancel_orders_for_symbol(symbol)
            exit_quote = client.get_latest_quote(symbol)
            exit_limit = round(exit_quote["bid"] - 0.05, 2)
            client.place_limit_order(symbol, trade.shares, OrderSide.SELL, exit_limit)
            _record_exit(
                trade=trade,
                exit_price=signal.entry_price,
                reason="manual_interrupt",
                trade_logger=trade_logger,
                client=client,
                symbol=symbol,
            )
        except Exception as exc:
            log.error("Error closing position on interrupt: %s", exc)

    trade_logger.print_summary()


# ------------------------------------------------------------------ #
# CLI                                                                 #
# ------------------------------------------------------------------ #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Premarket catalyst paper-trading bot (Alpaca paper account only).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "ticker",
        nargs="?",
        type=str,
        help="Stock ticker symbol, e.g. AAPL",
    )
    parser.add_argument(
        "--skip-filters",
        action="store_true",
        help="Bypass premarket filter checks for manually selected tickers",
    )
    parser.add_argument(
        "--skip-entry-signal",
        action="store_true",
        help="Bypass momentum confirmation (for testing filter logic only)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print trade history summary and exit",
    )

    args = parser.parse_args()
    config = Config()
    setup_logging(config.log_dir)

    if args.summary:
        TradeLogger(config.log_dir).print_summary()
        return

    if not args.ticker:
        parser.print_help()
        sys.exit(1)

    # Apply CLI overrides




    try:
        symbol = validate_ticker(args.ticker)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        run(
            symbol,
            config,
            skip_filters=args.skip_filters,
            skip_entry_signal=args.skip_entry_signal,
        )
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.getLogger("run_bot").exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
