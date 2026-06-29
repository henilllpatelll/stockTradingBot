"""
scripts/mock_day_run.py

Full mock trading day -- uses real market data, zero real orders.

Stages:
  1. Account   -- paper equity, buying power
  2. Universe  -- yfinance screener -> small-cap candidates
  3. Scan      -- PremarketScanner filters (price/float/RVOL/gap)
  4. Catalyst  -- Alpaca news + EDGAR per candidate
  5. Setup     -- premarket level marking + 8 pattern detectors
  6. Sizing    -- shares, stop, T1 for each GO setup
  7. Replay    -- walk real intraday bars through monitor logic (no orders placed)
  8. Summary   -- full decision table + simulated P&L

Run:
    python scripts/mock_day_run.py [--ai] [--symbols AAPL TSLA ...]
"""

from __future__ import annotations

import argparse
import logging
import sys
import os
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from alpaca_client import AlpacaClient
from config import Config
from risk_manager import RiskManager
from run_bot import _compute_atr
from scanner import CatalystChecker, PremarketScanner
from strategy import PremkLevels, mark_premarket_levels, run_setup_detectors

_ET = pytz.timezone("America/New_York")
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("mock_day_run")


# helpers

def _sep(char="-", width=72): print(char * width)

def _header(title: str):
    _sep("=")
    print(f"  {title}")
    _sep("=")

def _fmt_float(n: float) -> str:
    if n >= 1e9: return f"${n/1e9:.2f}B"
    if n >= 1e6: return f"${n/1e6:.1f}M"
    return f"${n:.2f}"


# -- Stage 1: Account ------------------------------------------------------- #

def stage_account(client: AlpacaClient) -> float:
    _header("STAGE 1 -- Account")
    try:
        acct = client.get_account()
        equity = float(acct.equity)
        buying_power = float(acct.buying_power)
        print(f"  Equity       : ${equity:,.2f}")
        print(f"  Buying power : ${buying_power:,.2f}")
        print(f"  Mode         : PAPER (no real money at risk)")
        return equity
    except Exception as exc:
        print(f"  [ERROR] Could not fetch account: {exc}")
        return 10_000.0


# -- Stage 2: Universe ------------------------------------------------------ #

def stage_universe(client: AlpacaClient, fixed: list[str] | None) -> list[str]:
    _header("STAGE 2 -- Universe")
    if fixed:
        print(f"  Fixed list   : {fixed}")
        return [s.upper() for s in fixed]
    print("  Source       : yfinance small_cap_gainers + most_actives")
    universe = client.get_market_universe(max_symbols=60)
    print(f"  Candidates   : {len(universe)} symbols")
    if universe:
        print(f"  Sample       : {universe[:10]}")
    return universe


# -- Stage 3: Scan ---------------------------------------------------------- #

def stage_scan(cfg: Config, client: AlpacaClient, universe: list[str]) -> list:
    _header("STAGE 3 -- Premarket Scanner")
    if not universe:
        print("  [SKIP] Empty universe -- market may be closed or screener returned nothing.")
        return []
    scanner = PremarketScanner(cfg, client)
    candidates = scanner.scan_once(universe)
    print(f"  Passed filters: {len(candidates)} / {len(universe)}")
    if candidates:
        _sep()
        print(f"  {'SYMBOL':<8}{'PRICE':>8}{'GAP%':>9}{'RVOL':>8}{'FLOAT':>10}  MKT CAP")
        _sep()
        for c in candidates:
            print(
                f"  {c.symbol:<8}{c.price:>8.2f}{c.gap_pct:>8.1f}%"
                f"{c.rvol:>7.1f}x{c.float_shares/1e6:>9.1f}M  {_fmt_float(c.market_cap)}"
            )
    return candidates


# -- Stage 4: Catalyst ------------------------------------------------------ #

def stage_catalyst(cfg: Config, client: AlpacaClient, candidates: list,
                   relaxed: bool = False) -> list:
    title = "STAGE 4 -- Catalyst Check"
    if relaxed:
        title += " [RELAXED -- 7-day lookback, mock fallback]"
    _header(title)
    checker = CatalystChecker(cfg.scanner, client)
    # In relaxed mode use a wider lookback to catch weekend gaps in news
    lookback = 168 if relaxed else cfg.scanner.news_lookback_hours  # 168h = 7 days
    results = []
    for c in candidates:
        headlines = checker.fetch_headlines(c.symbol, lookback)
        has_cat, headline = checker.has_catalyst(c.symbol, headlines)
        if not has_cat and relaxed:
            # Synthetic catalyst so all pipeline stages run during testing
            headline = f"[MOCK] Recent move in {c.symbol} -- catalyst assumed for test"
            has_cat = True
        verdict = "GO  " if has_cat else "SKIP"
        tag = "[MOCK]" if relaxed and "[MOCK]" in headline else ""
        short = (headline[:60] + "...") if len(headline) > 60 else headline
        print(f"  [{verdict}] {c.symbol:<8}  {tag} {short}".rstrip())
        if has_cat:
            c.headline = headline
            c.catalyst_confirmed = True
            results.append(c)
    print(f"\n  Catalyst GO   : {len(results)} / {len(candidates)}")
    return results


# -- Stage 5: Setup Detection ----------------------------------------------- #

def stage_setups(client: AlpacaClient, candidates: list, relaxed: bool = False) -> list:
    title = "STAGE 5 -- Setup Detection"
    if relaxed:
        title += " [RELAXED -- using last 48h bars as fallback]"
    _header(title)
    enriched = []
    for c in candidates:
        try:
            pm_bars = client.get_premarket_bars(c.symbol)

            # Relaxed fallback: use last 48h intraday bars as a proxy for premarket levels
            if pm_bars.empty and relaxed:
                pm_bars = client.get_intraday_bars(c.symbol, lookback_hours=48)
                if not pm_bars.empty:
                    print(f"  {c.symbol:<8}  [FALLBACK] using 48h bars as premarket proxy")

            if pm_bars.empty:
                print(f"  {c.symbol:<8}  [SKIP] no premarket bars")
                continue
            pm_levels = mark_premarket_levels(pm_bars)
            if pm_levels is None:
                print(f"  {c.symbol:<8}  [SKIP] could not build premarket levels")
                continue

            # Use last 48h bars for live bars too (fallback includes recent session)
            live_bars = client.get_intraday_bars(c.symbol, lookback_hours=48 if relaxed else 2)
            if live_bars.empty:
                print(f"  {c.symbol:<8}  [SKIP] no intraday bars")
                continue

            # Use last Friday's session open when in relaxed weekend mode
            now_et = datetime.now(_ET)
            if relaxed:
                # Walk back to find the most recent weekday
                from datetime import timedelta
                offset = (now_et.weekday() - 4) % 7  # days since last Friday
                last_friday = now_et - timedelta(days=offset)
                session_open = _ET.localize(
                    datetime(last_friday.year, last_friday.month, last_friday.day, 9, 30, 0)
                ).astimezone(timezone.utc)
            else:
                session_open = _ET.localize(
                    datetime(now_et.year, now_et.month, now_et.day, 9, 30, 0)
                ).astimezone(timezone.utc)

            results = run_setup_detectors(live_bars, pm_levels, c.prev_close, session_open)
            confirmed = [(name, trig, stop_ref) for ok, name, trig, stop_ref in results if ok]

            if not confirmed:
                print(f"  {c.symbol:<8}  [NO SETUP] pm_high=${pm_levels.premarket_high:.2f}"
                      f"  vwap=${pm_levels.vwap:.2f}")
                continue

            print(f"  {c.symbol:<8}  [SETUP OK] {[s[0] for s in confirmed]}"
                  f"  pm_high=${pm_levels.premarket_high:.2f}  vwap=${pm_levels.vwap:.2f}")
            enriched.append((c, pm_levels, live_bars, confirmed))

        except Exception as exc:
            print(f"  {c.symbol:<8}  [ERROR] {exc}")

    print(f"\n  Setup confirmed: {len(enriched)} / {len(candidates)}")
    return enriched


# -- Stage 6: Position Sizing ----------------------------------------------- #

def stage_sizing(cfg: Config, equity: float, enriched: list) -> list:
    _header("STAGE 6 -- Position Sizing")
    rm = RiskManager(cfg)
    sized = []
    for c, pm_levels, live_bars, setups in enriched:
        setup_name, trigger, stop_ref = setups[0]
        entry_price = trigger
        entry_bar_low = float(live_bars["low"].iloc[-1]) if not live_bars.empty else None
        # Enforce a minimum $0.10 stop offset so sizing stays sane in relaxed/proxy mode
        if stop_ref is not None and abs(entry_price - stop_ref) < 0.10:
            stop_ref = entry_price - 0.10
        if entry_bar_low is not None and abs(entry_price - entry_bar_low) < 0.10:
            entry_bar_low = entry_price - 0.10
        sizing = rm.calculate_position_size(
            equity, entry_price,
            entry_bar_low=entry_bar_low,
            setup_stop_ref=stop_ref,
        )
        targets = rm.calculate_targets(sizing.entry_price, sizing.initial_stop)
        print(
            f"  {c.symbol:<8}  setup={setup_name:<30}"
            f"  entry=${sizing.entry_price:.2f}"
            f"  stop=${sizing.initial_stop:.2f}"
            f"  T1=${targets.t1:.2f}"
            f"  shares={sizing.shares}"
            f"  risk=${sizing.risk_amount:.2f}"
        )
        sized.append((c, pm_levels, live_bars, setups, sizing, targets))
    return sized


# -- Stage 7: Bar-by-bar monitor replay ------------------------------------- #

_EXIT_ICONS = {
    "stop_loss":                 "[X] STOP",
    "breakeven_stop":            "[X] B/E STOP",
    "red_candle_exit":           "-> RED CANDLE",
    "extension_bar_exit":        "-> EXTENSION",
    "stall_exit":                "-> STALL",
    "momentum_fail_exit":        "-> MOM. FAIL",
    "max_hold_time":             "[T] MAX HOLD",
    "position_closed_externally":"? EXTERNAL",
}


def _replay(
    symbol: str, bars: pd.DataFrame, sizing, targets, rm: RiskManager, cfg: Config
) -> dict:
    """Walk 1-min bars through monitor logic. Returns a result dict."""
    if bars.empty or len(bars) < 2:
        return {"symbol": symbol, "outcome": "NO_DATA", "pnl": 0.0}

    entry_price = sizing.entry_price
    current_stop = sizing.initial_stop
    shares_remaining = sizing.shares
    t1_hit = False
    entry_bar_volume: Optional[float] = None
    start_idx = 0
    max_bars = cfg.max_hold_minutes  # 1 bar = 1 min

    events = []

    for i in range(start_idx, min(start_idx + max_bars, len(bars))):
        bar = bars.iloc[i]
        price = float(bar["close"])
        vol = float(bar["volume"])

        if entry_bar_volume is None:
            entry_bar_volume = vol

        # T1: sell 50%
        if not t1_hit and price >= targets.t1:
            t1_shares = shares_remaining // 2
            if t1_shares > 0:
                shares_remaining -= t1_shares
                current_stop = entry_price  # breakeven
            t1_hit = True
            events.append(f"bar{i}: T1 ${targets.t1:.2f} -- sold {t1_shares} shares, stop->breakeven")

        # Hard stop
        if rm.is_stop_hit(price, current_stop):
            reason = "breakeven_stop" if t1_hit else "stop_loss"
            pnl = (price - entry_price) * (sizing.shares - shares_remaining) + (price - entry_price) * shares_remaining
            return {"symbol": symbol, "outcome": reason, "exit_price": price,
                    "pnl": round(pnl, 2), "bar": i, "t1_hit": t1_hit, "events": events}

        # Post-T1 candle exits (need at least 1 prior bar)
        if t1_hit and i > 0:
            prev = bars.iloc[i - 1]
            atr = _compute_atr(bars.iloc[max(0, i - 14):i + 1])
            if rm.is_red_candle(prev):
                pnl = (price - entry_price) * sizing.shares
                return {"symbol": symbol, "outcome": "red_candle_exit", "exit_price": price,
                        "pnl": round(pnl, 2), "bar": i, "t1_hit": t1_hit, "events": events}
            if atr and rm.is_extension_bar(prev, atr):
                pnl = (price - entry_price) * sizing.shares
                return {"symbol": symbol, "outcome": "extension_bar_exit", "exit_price": price,
                        "pnl": round(pnl, 2), "bar": i, "t1_hit": t1_hit, "events": events}

        # Pre-T1 exits
        if not t1_hit and i >= cfg.risk.stall_bars:
            window = bars.iloc[start_idx:i + 1]
            if rm.is_stall(window, entry_price, targets.t1):
                pnl = (price - entry_price) * shares_remaining
                return {"symbol": symbol, "outcome": "stall_exit", "exit_price": price,
                        "pnl": round(pnl, 2), "bar": i, "t1_hit": t1_hit, "events": events}
            if entry_bar_volume and rm.is_momentum_fail(bars.iloc[max(0, i - 1):i + 1], entry_bar_volume):
                pnl = (price - entry_price) * shares_remaining
                return {"symbol": symbol, "outcome": "momentum_fail_exit", "exit_price": price,
                        "pnl": round(pnl, 2), "bar": i, "t1_hit": t1_hit, "events": events}

    # Max hold time reached
    final_price = float(bars["close"].iloc[min(start_idx + max_bars, len(bars)) - 1])
    pnl = (final_price - entry_price) * sizing.shares
    return {"symbol": symbol, "outcome": "max_hold_time", "exit_price": final_price,
            "pnl": round(pnl, 2), "bar": max_bars, "t1_hit": t1_hit, "events": events}


def stage_replay(cfg: Config, client: AlpacaClient, sized: list,
                 relaxed: bool = False) -> list:
    _header("STAGE 7 -- Bar-by-Bar Monitor Replay")
    rm = RiskManager(cfg)
    results = []
    # In relaxed/weekend mode pull 48h so we include Friday's session
    lookback = 48 if relaxed else 6
    for c, pm_levels, _bars, setups, sizing, targets in sized:
        try:
            replay_bars = client.get_intraday_bars(c.symbol, lookback_hours=lookback)
            if not replay_bars.empty:
                above = replay_bars[replay_bars["close"] >= sizing.entry_price * 0.99]
                if not above.empty:
                    replay_bars = replay_bars.loc[above.index[0]:]

            result = _replay(c.symbol, replay_bars, sizing, targets, rm, cfg)
        except Exception as exc:
            result = {"symbol": c.symbol, "outcome": "ERROR", "pnl": 0.0, "error": str(exc)}

        outcome = result.get("outcome", "?")
        exit_price = result.get("exit_price", sizing.entry_price)
        pnl = result.get("pnl", 0.0)
        t1 = "[OK] T1" if result.get("t1_hit") else "  "
        icon = _EXIT_ICONS.get(outcome, outcome)
        sign = "+" if pnl >= 0 else ""
        print(
            f"  {c.symbol:<8}  entry=${sizing.entry_price:.2f}"
            f"  exit=${exit_price:.2f}"
            f"  {t1}  {icon:<16}  PnL={sign}${pnl:.2f}"
        )
        for ev in result.get("events", []):
            print(f"           {ev}")
        result["catalyst"] = c.headline
        result["setup"] = setups[0][0]
        result["entry_price"] = sizing.entry_price
        result["shares"] = sizing.shares
        results.append(result)
    return results


# -- Stage 8: Summary ------------------------------------------------------- #

def stage_summary(universe: list, scan_candidates: list, catalyst_go: list,
                  setup_go: list, results: list) -> None:
    _header("STAGE 8 -- Mock Day Summary")
    wins = [r for r in results if r.get("pnl", 0) > 0]
    losses = [r for r in results if r.get("pnl", 0) <= 0]
    total_pnl = sum(r.get("pnl", 0) for r in results)

    print(f"  Universe scanned : {len(universe)}")
    print(f"  Passed filters   : {len(scan_candidates)}")
    print(f"  Catalyst GO      : {len(catalyst_go)}")
    print(f"  Setup confirmed  : {len(setup_go)}")
    print(f"  Simulated trades : {len(results)}")
    print()
    if results:
        wr = len(wins) / len(results) * 100 if results else 0
        print(f"  Win rate   : {wr:.0f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Total PnL  : {'+'if total_pnl>=0 else ''}${total_pnl:.2f}")
        avg = total_pnl / len(results)
        print(f"  Avg PnL    : {'+'if avg>=0 else ''}${avg:.2f}")
        print()
        _sep()
        print(f"  {'SYMBOL':<8}  {'SETUP':<30}  {'ENTRY':>8}  {'PNL':>10}  OUTCOME")
        _sep()
        for r in results:
            sign = "+" if r.get("pnl", 0) >= 0 else ""
            print(
                f"  {r['symbol']:<8}  {r.get('setup',''):<30}  "
                f"${r.get('entry_price',0):>7.2f}  "
                f"{sign}${r.get('pnl',0):>8.2f}  {r.get('outcome','')}"
            )
    else:
        print("  No trades reached the sizing/replay stage.")
        print("  (Market may be closed, or no setups confirmed for today.)")
    _sep("=")
    print(f"  Mock day run complete -- {datetime.now(_ET).strftime('%Y-%m-%d %H:%M:%S ET')}")
    _sep("=")


# -- Relaxed scan (bypass strict filters for weekend/testing) --------------- #

def stage_scan_relaxed(client: AlpacaClient, universe: list[str]) -> list:
    """Return GapperCandidates for all symbols that have any quote data.
    Used when market is closed or to force-exercise all pipeline stages."""
    from scanner import GapperCandidate
    _header("STAGE 3 -- Premarket Scanner [RELAXED -- filters bypassed]")
    candidates = []
    for symbol in universe:
        try:
            snap = client.get_snapshot(symbol)
            if snap is None:
                continue
            price = float(snap.latest_trade.price) if snap.latest_trade else 0.0
            prev_close = float(snap.previous_daily_bar.close) if snap.previous_daily_bar else 0.0
            if not price or not prev_close:
                continue
            gap_pct = (price - prev_close) / prev_close * 100
            vol = float(snap.daily_bar.volume) if snap.daily_bar else 0.0
            candidates.append(GapperCandidate(
                symbol=symbol,
                price=price,
                prev_close=prev_close,
                gap_pct=round(gap_pct, 2),
                rvol=0.0,
                float_shares=5_000_000,  # unknown — assume small float for sizing
                market_cap=0.0,
                scan_time=datetime.now(_ET).strftime("%H:%M:%S ET"),
            ))
        except Exception as exc:
            print(f"  {symbol:<8}  [SKIP] {exc}")

    print(f"  Symbols with data : {len(candidates)} / {len(universe)}")
    if candidates:
        _sep()
        print(f"  {'SYMBOL':<8}{'PRICE':>8}{'GAP% vs prev session':>24}")
        _sep()
        for c in candidates:
            sign = "+" if c.gap_pct >= 0 else ""
            print(f"  {c.symbol:<8}{c.price:>8.2f}  {sign}{c.gap_pct:.2f}% vs ${c.prev_close:.2f}")
    return candidates


# -- Main ------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Mock trading day -- real data, no orders.")
    parser.add_argument("--symbols", nargs="+", help="Fixed ticker list (skip screener)")
    parser.add_argument("--relaxed", action="store_true",
                        help="Bypass strict scanner filters (use when market is closed or for testing)")
    parser.add_argument("--verbose", action="store_true", help="Show INFO-level logs")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    cfg = Config()
    client = AlpacaClient(cfg)

    print()
    print("  +==================================================+")
    print("  |        WARRIOR BOT -- MOCK DAY RUN               |")
    print("  |   Real data | Paper logic | Zero real orders      |")
    print("  +==================================================+")
    print()

    equity = stage_account(client)
    print()
    universe = stage_universe(client, args.symbols)
    print()

    if args.relaxed:
        scan_candidates = stage_scan_relaxed(client, universe)
    else:
        scan_candidates = stage_scan(cfg, client, universe)
    print()

    if not scan_candidates:
        print("  No scanner candidates -- stopping early.")
        if not args.relaxed:
            print("  Tip: market may be closed. Re-run with --relaxed to bypass filters.")
        stage_summary(universe, scan_candidates, [], [], [])
        return

    catalyst_go = stage_catalyst(cfg, client, scan_candidates, relaxed=args.relaxed)
    print()

    if not catalyst_go:
        print("  No catalyst-confirmed candidates -- stopping early.")
        stage_summary(universe, scan_candidates, catalyst_go, [], [])
        return

    setup_go = stage_setups(client, catalyst_go, relaxed=args.relaxed)
    print()

    if not setup_go:
        print("  No confirmed setups -- stopping early.")
        stage_summary(universe, scan_candidates, catalyst_go, setup_go, [])
        return

    sized = stage_sizing(cfg, equity, setup_go)
    print()
    results = stage_replay(cfg, client, sized, relaxed=args.relaxed)
    print()
    stage_summary(universe, scan_candidates, catalyst_go, setup_go, results)


if __name__ == "__main__":
    main()
