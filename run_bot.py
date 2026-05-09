"""
run_bot.py — CLI entry point for the premarket catalyst paper-trading bot.

Usage:
    python run_bot.py TICKER [options]

Examples:
    python run_bot.py BBAI
    python run_bot.py MSTR --stop-loss 8 --trail 6
    python run_bot.py --summary
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from alpaca.trading.enums import OrderSide

from alpaca_client import AlpacaClient
from config import Config
from risk_manager import PositionSizing, RiskManager
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
    trailing_stop_placed: bool,
) -> None:
    log = logging.getLogger("monitor")
    symbol = trade.symbol
    current_stop = sizing.initial_stop
    start = time.monotonic()
    interval = config.monitor_interval_seconds
    max_secs = config.max_hold_minutes * 60

    log.info(
        "Monitoring %s | entry=$%.2f  stop=$%.2f  trail=%.0f%%  max_hold=%dm",
        symbol,
        sizing.entry_price,
        current_stop,
        config.risk.trailing_stop_pct * 100,
        config.max_hold_minutes,
    )

    while True:
        elapsed = time.monotonic() - start

        position = client.get_position(symbol)

        # Position closed externally — Alpaca's trailing stop was triggered.
        if position is None:
            log.info("%s: position closed externally (trailing stop filled)", symbol)
            _record_exit(
                trade=trade,
                exit_price=sizing.entry_price,  # rough fallback
                reason="trailing_stop_filled",
                trade_logger=trade_logger,
                client=client,
                symbol=symbol,
            )
            break

        current_price = float(position.current_price)
        unrealized = float(position.unrealized_pl)

        # Update manual trailing stop (even when Alpaca stop is active, track it)
        new_stop = risk_mgr.update_trailing_stop(current_price, current_stop)
        if new_stop > current_stop:
            log.info(
                "%s: trailing stop raised $%.2f → $%.2f",
                symbol,
                current_stop,
                new_stop,
            )
            current_stop = new_stop

        log.info(
            "%s  price=$%.2f  stop=$%.2f  unrealized=${:+.2f}  elapsed=%.0fm".format(
                unrealized
            ),
            symbol,
            current_price,
            current_stop,
            elapsed / 60,
        )

        # Hard stop hit — close manually (belt-and-suspenders if native order failed)
        if not trailing_stop_placed and risk_mgr.is_stop_hit(current_price, current_stop):
            log.warning(
                "%s: stop hit  price=$%.2f  stop=$%.2f — closing",
                symbol,
                current_price,
                current_stop,
            )
            try:
                client.close_position(symbol)
            except Exception:
                pass
            _record_exit(
                trade=trade,
                exit_price=current_price,
                reason="stop_loss",
                trade_logger=trade_logger,
                client=client,
                symbol=symbol,
            )
            break

        # Max hold time reached
        if elapsed >= max_secs:
            log.info(
                "%s: max hold time (%dm) reached — closing", symbol, config.max_hold_minutes
            )
            try:
                client.cancel_orders_for_symbol(symbol)
                client.close_position(symbol)
            except Exception:
                pass
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

def run(symbol: str, config: Config, skip_entry_signal: bool = False) -> None:
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
    log.info("=== Running premarket filters for %s ===", symbol)
    filter_result: FilterResult = strategy.run_filters(symbol)
    print(f"\n[{symbol}] Filter results:\n{filter_result.summary()}")

    if not filter_result.passed:
        print(f"\n{symbol} did not pass all filters — no trade placed.\n")
        trade_logger.print_summary()
        return

    log.info("%s passed all filters.", symbol)

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
    entry_order = client.place_market_order(symbol, sizing.shares, OrderSide.BUY)
    log.info("Entry order submitted: id=%s", entry_order.id)

    # Paper fills are near-instant; wait briefly before placing the stop
    time.sleep(2)

    trailing_stop_placed = False
    trail_pct = config.risk.trailing_stop_pct * 100
    try:
        client.place_trailing_stop(symbol, sizing.shares, trail_pct)
        trailing_stop_placed = True
    except Exception as exc:
        log.warning(
            "Could not place Alpaca trailing stop (%s) — will manage stop manually.", exc
        )
        # Fall back to a plain stop order at the initial stop price
        try:
            client.place_stop_order(symbol, sizing.shares, sizing.initial_stop)
        except Exception as exc2:
            log.warning("Plain stop order also failed (%s) — monitoring manually.", exc2)

    # ---- Record entry --------------------------------------------
    trade = TradeRecord(
        symbol=symbol,
        entry_time=_now_iso(),
        entry_price=signal.entry_price,
        shares=sizing.shares,
        initial_stop=sizing.initial_stop,
        trail_pct=config.risk.trailing_stop_pct,
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
        f"  Stop loss   : ${sizing.initial_stop:.2f}  ({config.risk.stop_loss_pct * 100:.0f}%)\n"
        f"  Trail stop  : {trail_pct:.0f}%\n"
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
            trailing_stop_placed=trailing_stop_placed,
        )
    except KeyboardInterrupt:
        log.info("Interrupted — closing %s", symbol)
        try:
            client.cancel_orders_for_symbol(symbol)
            client.close_position(symbol)
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
        "--stop-loss",
        type=float,
        metavar="PCT",
        help="Hard stop loss percentage (5–10 recommended), e.g. --stop-loss 7",
    )
    parser.add_argument(
        "--trail",
        type=float,
        metavar="PCT",
        help="Trailing stop percentage, e.g. --trail 5",
    )
    parser.add_argument(
        "--max-position",
        type=float,
        metavar="PCT",
        help="Max position size as %% of account equity, e.g. --max-position 2",
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
    if args.stop_loss:
        config.risk.stop_loss_pct = args.stop_loss / 100
    if args.trail:
        config.risk.trailing_stop_pct = args.trail / 100
    if args.max_position:
        config.risk.max_position_pct = args.max_position / 100

    try:
        symbol = validate_ticker(args.ticker)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        run(symbol, config, skip_entry_signal=args.skip_entry_signal)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.getLogger("run_bot").exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
