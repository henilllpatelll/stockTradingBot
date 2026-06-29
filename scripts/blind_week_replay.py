"""
Blind historical replay for the catalyst bot.

This script walks Benzinga-style headline events from a saved news file and
simulates decisions as if the clock were at each headline/scan time. Future
bars are only used after a trade has already been accepted, for the replay.

Run:
    python scripts/blind_week_replay.py --start 2026-05-11 --end 2026-05-15
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz
import yfinance as yf
from alpaca.data.enums import Adjustment
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpaca_client import AlpacaClient
from config import Config
from risk_manager import RiskManager
from run_bot import _compute_atr, _entry_signal_quality, _learned_entry_gate
from strategy import EntrySignal, FilterResult, mark_premarket_levels, run_setup_detectors

_ET = pytz.timezone("America/New_York")
_DATE_RE = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday) May (1[1-5]), 2026$")
_TIME_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})(AM|PM)$")
_SYMBOL_RE = re.compile(r"^([A-Z]{1,5})(?:\s+\+\d+)?$")


@dataclass(frozen=True)
class NewsEvent:
    day: date
    published_at: datetime
    symbol: str
    headline: str


@dataclass
class ReplayDecision:
    day: date
    symbol: str
    decision_time: datetime
    headline_time: datetime
    headline: str
    status: str
    reason: str
    price: float = 0.0
    gap_pct: float = 0.0
    rvol: float = 0.0
    setup: str = ""
    entry: float = 0.0
    stop: float = 0.0
    t1: float = 0.0
    shares: int = 0
    exit_price: float = 0.0
    pnl: float = 0.0
    outcome: str = ""


class NeutralPerformanceTracker:
    def adjusted_threshold(self, _setup_name: str, base: float) -> float:
        return base


def parse_news_events(path: Path) -> list[NewsEvent]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()]
    events: list[NewsEvent] = []
    current_day: Optional[date] = None
    i = 0
    while i < len(lines):
        line = lines[i]
        date_match = _DATE_RE.match(line)
        if date_match:
            current_day = date(2026, 5, int(date_match.group(2)))
            i += 1
            continue

        time_match = _TIME_RE.match(line)
        if current_day and time_match and i + 2 < len(lines):
            symbol_match = _SYMBOL_RE.match(lines[i + 1])
            if symbol_match:
                published_at = _localize_event_time(current_day, time_match)
                headline = lines[i + 2]
                events.append(
                    NewsEvent(
                        day=current_day,
                        published_at=published_at,
                        symbol=symbol_match.group(1),
                        headline=headline,
                    )
                )
                i += 3
                continue
        i += 1

    events.sort(key=lambda event: event.published_at)
    return events


def decision_time_for(event: NewsEvent, cfg: Config) -> Optional[datetime]:
    start = _ET.localize(datetime.combine(event.day, time(cfg.trading_start_hour_et, 0)))
    end = _ET.localize(datetime.combine(event.day, time(cfg.trading_end_hour_et, 0)))
    decision_time = max(event.published_at, start)
    if decision_time >= end:
        return None
    return decision_time


def _localize_event_time(day: date, match: re.Match[str]) -> datetime:
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3))
    meridiem = match.group(4)
    if meridiem == "PM" and hour != 12:
        hour += 12
    if meridiem == "AM" and hour == 12:
        hour = 0
    return _ET.localize(datetime.combine(day, time(hour, minute, second)))


def _session_bounds(day: date) -> tuple[datetime, datetime]:
    start = _ET.localize(datetime.combine(day, time(4, 0)))
    end = _ET.localize(datetime.combine(day, time(20, 0)))
    return start, end


def _to_df(bars_response, symbol: str) -> pd.DataFrame:
    df = bars_response.df
    if df.empty:
        return df
    if isinstance(df.index, pd.MultiIndex):
        try:
            df = df.loc[symbol]
        except KeyError:
            return pd.DataFrame()
    return df.sort_index()


def fetch_minute_bars(client: AlpacaClient, symbol: str, day: date) -> pd.DataFrame:
    start_et, end_et = _session_bounds(day)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start_et.astimezone(timezone.utc),
        end=end_et.astimezone(timezone.utc),
        adjustment=Adjustment.RAW,
        feed="sip",
    )
    return _to_df(client._data.get_stock_bars(req), symbol)


def fetch_daily_context(client: AlpacaClient, symbol: str, day: date) -> tuple[float, float]:
    # Daily bars are timestamped at midnight ET. End just before the session date
    # so the replay cannot see the current day's completed candle.
    end_et = _ET.localize(datetime.combine(day, time(0, 0))) - timedelta(seconds=1)
    start_et = end_et - timedelta(days=60)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start_et.astimezone(timezone.utc),
        end=end_et.astimezone(timezone.utc),
        adjustment=Adjustment.RAW,
        feed="sip",
    )
    daily = _to_df(client._data.get_stock_bars(req), symbol)
    if daily.empty:
        return 0.0, 0.0
    prev_close = float(daily["close"].iloc[-1])
    avg_volume = float(daily["volume"].tail(30).mean())
    return prev_close, avg_volume


def fetch_current_fundamentals(symbol: str) -> tuple[float, float]:
    try:
        info = yf.Ticker(symbol).info
    except Exception:
        return 0.0, 0.0
    float_shares = float(info.get("floatShares") or info.get("sharesOutstanding") or 0)
    market_cap = float(info.get("marketCap") or 0)
    return float_shares, market_cap


def evaluate_filters(
    cfg: Config,
    bars_until_decision: pd.DataFrame,
    prev_close: float,
    avg_volume: float,
    float_shares: float,
    market_cap: float,
) -> FilterResult:
    result = FilterResult(passed=False)
    failures: list[str] = []
    if bars_until_decision.empty:
        result.failures = ["no bars visible yet"]
        return result

    price = float(bars_until_decision["close"].iloc[-1])
    result.price = price
    result.prev_close = prev_close
    result.float_shares = float_shares
    result.market_cap = market_cap

    if not (cfg.filters.min_price <= price <= cfg.filters.max_price):
        failures.append(f"price ${price:.2f} outside ${cfg.filters.min_price:.2f}-${cfg.filters.max_price:.2f}")
    if float_shares <= 0:
        failures.append("float unavailable")
    elif float_shares > cfg.filters.max_float_shares:
        failures.append(f"float {float_shares / 1e6:.1f}M > {cfg.filters.max_float_shares / 1e6:.1f}M")
    if market_cap > cfg.filters.max_market_cap:
        failures.append(f"market cap ${market_cap / 1e9:.2f}B > ${cfg.filters.max_market_cap / 1e9:.2f}B")
    if prev_close <= 0:
        failures.append("previous close unavailable")
    else:
        result.change_pct = (price - prev_close) / prev_close * 100
        if result.change_pct < cfg.filters.min_change_pct:
            failures.append(f"gap {result.change_pct:.1f}% < {cfg.filters.min_change_pct:.1f}%")
    if avg_volume <= 0:
        failures.append("average volume unavailable")
    else:
        elapsed_minutes = len(bars_until_decision)
        expected = avg_volume / 390 * elapsed_minutes
        result.rvol = float(bars_until_decision["volume"].sum()) / expected if expected > 0 else 0.0
        if result.rvol < cfg.filters.min_rvol:
            failures.append(f"RVOL {result.rvol:.2f} < {cfg.filters.min_rvol:.1f}")

    result.failures = failures
    result.passed = len(failures) == 0
    return result


def check_entry_signal(symbol: str, filter_result: FilterResult, bars_until_decision: pd.DataFrame) -> Optional[EntrySignal]:
    if not filter_result.passed or len(bars_until_decision) < 6:
        return None
    recent = bars_until_decision.tail(5)
    prior = bars_until_decision.iloc[:-5]
    prior_avg = prior["volume"].mean() if not prior.empty else recent["volume"].mean()
    vol_acceleration = float(recent["volume"].mean() / prior_avg) if prior_avg > 0 else 1.0
    recent_lows = recent["low"].values
    price_strength = bool(recent_lows[-1] >= recent_lows[:-1].mean())
    if vol_acceleration < 1.2 or not price_strength:
        return None
    return EntrySignal(
        symbol=symbol,
        entry_price=float(bars_until_decision["close"].iloc[-1]),
        reason=f"vol_accel={vol_acceleration:.2f}x, change={filter_result.change_pct:+.1f}%, RVOL={filter_result.rvol:.2f}",
        rvol=filter_result.rvol,
        change_pct=filter_result.change_pct,
        vol_acceleration=vol_acceleration,
    )


def replay_trade(cfg: Config, bars_after_entry: pd.DataFrame, sizing, targets) -> tuple[str, float, float]:
    rm = RiskManager(cfg)
    if bars_after_entry.empty:
        return "NO_DATA_AFTER_ENTRY", sizing.entry_price, 0.0

    entry = sizing.entry_price
    current_stop = sizing.initial_stop
    shares_remaining = sizing.shares
    realized = 0.0
    t1_hit = False
    entry_bar_volume: Optional[float] = None
    held = pd.DataFrame()

    for i, (_, bar) in enumerate(bars_after_entry.head(cfg.max_hold_minutes).iterrows()):
        price = float(bar["close"])
        volume = float(bar["volume"])
        if entry_bar_volume is None:
            entry_bar_volume = volume
        held = pd.concat([held, pd.DataFrame([bar])], ignore_index=True)

        if not t1_hit and price >= targets.t1:
            t1_shares = shares_remaining // 2
            if t1_shares > 0:
                realized += (targets.t1 - entry) * t1_shares
                shares_remaining -= t1_shares
                current_stop = entry
            t1_hit = True

        if rm.is_stop_hit(price, current_stop):
            pnl = realized + (price - entry) * shares_remaining
            reason = "breakeven_stop" if t1_hit else "stop_loss"
            return reason, price, round(pnl, 2)

        if t1_hit and i > 0:
            prev = bars_after_entry.iloc[i - 1]
            atr = _compute_atr(bars_after_entry.iloc[max(0, i - 14):i + 1])
            if rm.is_red_candle(prev):
                pnl = realized + (price - entry) * shares_remaining
                return "red_candle_exit", price, round(pnl, 2)
            if atr and rm.is_extension_bar(prev, atr):
                pnl = realized + (price - entry) * shares_remaining
                return "extension_bar_exit", price, round(pnl, 2)

        if not t1_hit and len(held) >= cfg.risk.stall_bars:
            if rm.is_stall(held, entry, targets.t1):
                pnl = (price - entry) * shares_remaining
                return "stall_exit", price, round(pnl, 2)
            if entry_bar_volume and rm.is_momentum_fail(held, entry_bar_volume):
                pnl = (price - entry) * shares_remaining
                return "momentum_fail_exit", price, round(pnl, 2)

    final_price = float(bars_after_entry["close"].head(cfg.max_hold_minutes).iloc[-1])
    pnl = realized + (final_price - entry) * shares_remaining
    return "max_hold_time", final_price, round(pnl, 2)


def simulate_event(
    cfg: Config,
    client: AlpacaClient,
    event: NewsEvent,
    bars_cache: dict[tuple[str, date], pd.DataFrame],
    daily_cache: dict[tuple[str, date], tuple[float, float]],
    fundamentals_cache: dict[str, tuple[float, float]],
) -> ReplayDecision:
    decision_time = decision_time_for(event, cfg)
    if decision_time is None:
        return ReplayDecision(
            day=event.day,
            symbol=event.symbol,
            decision_time=event.published_at,
            headline_time=event.published_at,
            headline=event.headline,
            status="SKIP",
            reason="outside 7:00-11:00 ET trading window",
        )

    key = (event.symbol, event.day)
    bars = bars_cache.setdefault(key, fetch_minute_bars(client, event.symbol, event.day))
    prev_close, avg_volume = daily_cache.setdefault(key, fetch_daily_context(client, event.symbol, event.day))
    float_shares, market_cap = fundamentals_cache.setdefault(event.symbol, fetch_current_fundamentals(event.symbol))

    decision_utc = decision_time.astimezone(timezone.utc)
    idx = bars.index.tz_convert("UTC") if bars.index.tz is not None else bars.index.tz_localize("UTC")
    bars_until = bars[idx <= pd.Timestamp(decision_utc)]
    filter_result = evaluate_filters(cfg, bars_until, prev_close, avg_volume, float_shares, market_cap)
    if not filter_result.passed:
        return ReplayDecision(
            day=event.day,
            symbol=event.symbol,
            decision_time=decision_time,
            headline_time=event.published_at,
            headline=event.headline,
            status="SKIP",
            reason="; ".join(filter_result.failures),
            price=filter_result.price,
            gap_pct=filter_result.change_pct,
            rvol=filter_result.rvol,
        )

    premarket_end = _ET.localize(datetime.combine(event.day, time(9, 29, 59))).astimezone(timezone.utc)
    pm_idx = idx <= pd.Timestamp(min(decision_utc, premarket_end))
    pm_levels = mark_premarket_levels(bars[pm_idx])
    setup_results = run_setup_detectors(bars_until, pm_levels, filter_result.prev_close, _ET.localize(datetime.combine(event.day, time(9, 30))).astimezone(timezone.utc))
    active_setups = [(name, trigger, stop_ref) for ok, name, trigger, stop_ref in setup_results if ok]
    if not active_setups:
        return ReplayDecision(
            day=event.day,
            symbol=event.symbol,
            decision_time=decision_time,
            headline_time=event.published_at,
            headline=event.headline,
            status="SKIP",
            reason="no confirmed setup pattern",
            price=filter_result.price,
            gap_pct=filter_result.change_pct,
            rvol=filter_result.rvol,
        )

    signal = check_entry_signal(event.symbol, filter_result, bars_until)
    if signal is None:
        return ReplayDecision(
            day=event.day,
            symbol=event.symbol,
            decision_time=decision_time,
            headline_time=event.published_at,
            headline=event.headline,
            status="SKIP",
            reason="entry signal not confirmed",
            price=filter_result.price,
            gap_pct=filter_result.change_pct,
            rvol=filter_result.rvol,
            setup=active_setups[0][0],
        )
    signal.setup_name = active_setups[0][0]
    signal.setup_stop_ref = active_setups[0][2]
    learned_ok, quality, threshold = _learned_entry_gate(signal, cfg, NeutralPerformanceTracker())
    if not learned_ok:
        return ReplayDecision(
            day=event.day,
            symbol=event.symbol,
            decision_time=decision_time,
            headline_time=event.published_at,
            headline=event.headline,
            status="SKIP",
            reason=f"learned entry gate rejected quality {quality:.0%} < threshold {threshold:.0%}",
            price=filter_result.price,
            gap_pct=filter_result.change_pct,
            rvol=filter_result.rvol,
            setup=active_setups[0][0],
        )

    rm = RiskManager(cfg)
    entry_bar_low = float(bars_until["low"].iloc[-1])
    sizing = rm.calculate_position_size(
        100_000,
        signal.entry_price,
        entry_bar_low=entry_bar_low,
        setup_stop_ref=signal.setup_stop_ref,
    )
    targets = rm.calculate_targets(sizing.entry_price, sizing.initial_stop)
    bars_after = bars[idx > pd.Timestamp(decision_utc)]
    outcome, exit_price, pnl = replay_trade(cfg, bars_after, sizing, targets)
    return ReplayDecision(
        day=event.day,
        symbol=event.symbol,
        decision_time=decision_time,
        headline_time=event.published_at,
        headline=event.headline,
        status="TRADE",
        reason=signal.reason + f", quality={_entry_signal_quality(signal, cfg):.0%}",
        price=filter_result.price,
        gap_pct=filter_result.change_pct,
        rvol=filter_result.rvol,
        setup=active_setups[0][0],
        entry=sizing.entry_price,
        stop=sizing.initial_stop,
        t1=targets.t1,
        shares=sizing.shares,
        exit_price=exit_price,
        pnl=pnl,
        outcome=outcome,
    )


def print_summary(decisions: list[ReplayDecision]) -> None:
    print("\nBLIND WEEK REPLAY: 2026-05-11 through 2026-05-15")
    print("Universe: ticker_symbols_benpro.md headlines; decisions only use bars visible at each decision time.")
    print("Risk: Config defaults, $500 risk bucket, 30-minute max hold, no real orders.\n")

    for day in sorted({d.day for d in decisions}):
        day_rows = [d for d in decisions if d.day == day]
        trades = [d for d in day_rows if d.status == "TRADE"]
        pnl = sum(d.pnl for d in trades)
        print(f"{day.isoformat()}  events={len(day_rows)}  trades={len(trades)}  PnL={pnl:+.2f}")
        for d in day_rows:
            hhmm = d.decision_time.astimezone(_ET).strftime("%H:%M:%S")
            if d.status == "TRADE":
                print(
                    f"  {hhmm} {d.symbol:<5} TRADE {d.setup:<24} "
                    f"entry={d.entry:.2f} stop={d.stop:.2f} T1={d.t1:.2f} "
                    f"shares={d.shares:<5} exit={d.exit_price:.2f} "
                    f"PnL={d.pnl:+.2f} {d.outcome}"
                )
            else:
                print(
                    f"  {hhmm} {d.symbol:<5} SKIP  price={d.price:.2f} "
                    f"gap={d.gap_pct:+.1f}% rvol={d.rvol:.2f}  {d.reason}"
                )
        print()

    trades = [d for d in decisions if d.status == "TRADE"]
    wins = [d for d in trades if d.pnl > 0]
    total = sum(d.pnl for d in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0.0
    print(f"TOTAL: events={len(decisions)} trades={len(trades)} win_rate={win_rate:.0f}% PnL={total:+.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Blind replay a historical headline-driven trading week.")
    parser.add_argument("--start", default="2026-05-11")
    parser.add_argument("--end", default="2026-05-15")
    parser.add_argument("--news-file", default="ticker_symbols_benpro.md")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    cfg = Config()
    client = AlpacaClient(cfg)
    events = [
        event for event in parse_news_events(Path(args.news_file))
        if start <= event.day <= end
    ]

    bars_cache: dict[tuple[str, date], pd.DataFrame] = {}
    daily_cache: dict[tuple[str, date], tuple[float, float]] = {}
    fundamentals_cache: dict[str, tuple[float, float]] = {}
    decisions: list[ReplayDecision] = []
    traded_symbols: set[tuple[str, date]] = set()
    for event in events:
        if (event.symbol, event.day) in traded_symbols:
            continue
        decision = simulate_event(cfg, client, event, bars_cache, daily_cache, fundamentals_cache)
        decisions.append(decision)
        if decision.status == "TRADE":
            traded_symbols.add((event.symbol, event.day))

    print_summary(decisions)


if __name__ == "__main__":
    main()
