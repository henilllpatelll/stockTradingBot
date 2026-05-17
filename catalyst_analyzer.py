"""
Benzinga Pro Catalyst Analyzer
Reads ticker_symbols_benpro.md, fetches Alpaca SIP bars around each
catalyst, and prints a formatted analysis table with TRADEABLE signals.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

# Ensure UTF-8 output on Windows so symbols like ↑ ✓ render correctly
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
import pytz
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv

load_dotenv()

ET = pytz.timezone("America/New_York")
UTC = pytz.utc

data_client = StockHistoricalDataClient(
    api_key=os.getenv("ALPACA_API_KEY"),
    secret_key=os.getenv("ALPACA_SECRET_KEY"),
)

TF_1MIN = TimeFrame.Minute

INPUT_FILE = "ticker_symbols_benpro.md"

SKIP_TICKERS = {"ARAI", "ADUR", "ACOG", "ABEO", "ABOS", "ARXS", "APA", "INHD"}


@dataclass
class NewsEvent:
    dt_utc: object
    symbol: str
    headline: str
    source: str
    date_label: str


# ── Parsing ──────────────────────────────────────────────────────────────────

_TIME_RE = re.compile(r"^(\d{1,2}:\d{2}:\d{2})(AM|PM)$")
_DATE_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) \w+ \d+, \d{4}$"
)
_TICKER_RE = re.compile(r"^([A-Z]{2,6})(?:\s+\+\d+)?$")
_SOURCES = {"BZ Wire", "PRN", "GLN", "BW"}


def parse_events(path: str) -> list[NewsEvent]:
    events: list[NewsEvent] = []
    current_date_label = ""
    current_date = None

    with open(path, encoding="utf-8") as f:
        lines = [l.rstrip("\n") for l in f]

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if _DATE_RE.match(line):
            current_date_label = line
            # parse date from label, e.g. "Friday May 15, 2026"
            parts = line.split()  # ['Friday', 'May', '15,', '2026']
            month_day_year = " ".join(parts[1:])  # "May 15, 2026"
            current_date = pd.to_datetime(month_day_year, format="%B %d, %Y").date()
            i += 1
            continue

        m_time = _TIME_RE.match(line)
        if m_time and current_date is not None:
            time_str = m_time.group(1)
            ampm = m_time.group(2)
            # next line: ticker
            if i + 1 >= len(lines):
                i += 1
                continue
            ticker_line = lines[i + 1].strip()
            m_tick = _TICKER_RE.match(ticker_line)
            if not m_tick:
                i += 1
                continue
            symbol = m_tick.group(1)

            # skip high-count sector sweeps
            if symbol in SKIP_TICKERS:
                i += 1
                continue

            # skip if ticker line has a very large +N count (sector sweeps)
            count_match = re.search(r"\+(\d+)$", ticker_line)
            if count_match and int(count_match.group(1)) > 9:
                i += 1
                continue

            # next line: headline
            if i + 2 >= len(lines):
                i += 1
                continue
            headline = lines[i + 2].strip()

            # skip generic earnings scheduled entries
            if headline.startswith("Earnings Scheduled For"):
                i += 1
                continue

            # next line: source
            source = ""
            if i + 3 < len(lines) and lines[i + 3].strip() in _SOURCES:
                source = lines[i + 3].strip()

            # parse datetime in ET then convert to UTC
            dt_naive_str = f"{current_date} {time_str} {ampm}"
            dt_et = ET.localize(
                pd.to_datetime(dt_naive_str, format="%Y-%m-%d %I:%M:%S %p").to_pydatetime()
            )
            dt_utc = dt_et.astimezone(UTC)

            events.append(
                NewsEvent(
                    dt_utc=dt_utc,
                    symbol=symbol,
                    headline=headline,
                    source=source,
                    date_label=current_date_label,
                )
            )
            i += 1
            continue

        i += 1

    return events


# ── Market open helper ────────────────────────────────────────────────────────

def market_open_utc(dt_utc) -> object:
    local = dt_utc.astimezone(ET)
    naive = local.replace(hour=9, minute=30, second=0, microsecond=0, tzinfo=None)
    open_et = ET.localize(naive)
    return open_et.astimezone(UTC)


# ── Bar fetching ──────────────────────────────────────────────────────────────

def fetch_bars(event: NewsEvent):
    symbol = event.symbol
    is_premarket = event.dt_utc < market_open_utc(event.dt_utc)

    end_1m = (
        market_open_utc(event.dt_utc)
        if is_premarket
        else event.dt_utc + timedelta(minutes=10)
    )

    df_1m = pd.DataFrame()

    try:
        req_1m = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TF_1MIN,
            start=event.dt_utc - timedelta(minutes=5),
            end=end_1m,
            feed="sip",
        )
        raw_1m = data_client.get_stock_bars(req_1m).df
        if not raw_1m.empty:
            df_1m = raw_1m.reset_index()
            if "symbol" in df_1m.columns:
                df_1m = df_1m[df_1m["symbol"] == symbol].copy()
            df_1m = df_1m.sort_values("timestamp").reset_index(drop=True)
    except Exception:
        pass

    time.sleep(0.2)
    return df_1m, is_premarket


# ── Metric helpers ────────────────────────────────────────────────────────────

def _closest_bar(df: pd.DataFrame, target):
    if df.empty:
        return None
    idx = (df["timestamp"] - target).abs().idxmin()
    return df.loc[idx]


def _pct(new_val, base_val) -> Optional[float]:
    if base_val is None or base_val == 0:
        return None
    try:
        return (float(new_val) - float(base_val)) / float(base_val) * 100.0
    except Exception:
        return None


def compute_metrics(event: NewsEvent, df_1m: pd.DataFrame, is_premarket: bool) -> dict:
    m: dict = {}
    # 10-sec bars unsupported by this alpaca-py build; columns will show as —
    m["move_10s"] = m["move_30s"] = None

    # ── 1-minute metrics ─────────────────────────────────────────────────────
    if not df_1m.empty:
        ts = event.dt_utc
        pre_rows_1m = df_1m[df_1m["timestamp"] <= ts]
        pre_1m = pre_rows_1m.iloc[-1] if not pre_rows_1m.empty else None
        pre_close_1m = float(pre_1m["close"]) if pre_1m is not None else None

        after_1m = df_1m[df_1m["timestamp"] > ts]
        t1m_bar = after_1m.iloc[0] if not after_1m.empty else None
        t5m_bar = _closest_bar(df_1m, ts + timedelta(minutes=5))

        # vol spike: t1m volume / mean of last 5 pre bars
        pre_vols = pre_rows_1m["volume"].tail(5)
        avg_vol = pre_vols.mean() if len(pre_vols) > 0 else 0
        t1m_vol = float(t1m_bar["volume"]) if t1m_bar is not None else None
        m["vol_spike"] = (t1m_vol / avg_vol) if (t1m_vol is not None and avg_vol > 0) else None

        m["move_1m"] = _pct(t1m_bar["close"], pre_close_1m) if t1m_bar is not None else None
        m["move_5m"] = _pct(t5m_bar["close"], pre_close_1m) if t5m_bar is not None else None

        if is_premarket:
            open_utc = market_open_utc(event.dt_utc)
            open_rows = df_1m[df_1m["timestamp"] <= open_utc]
            open_bar = open_rows.iloc[-1] if not open_rows.empty else None
            m["move_to_open"] = _pct(open_bar["close"], pre_close_1m) if open_bar is not None else None
            window = df_1m[df_1m["timestamp"] >= ts]
            window = window[window["timestamp"] <= open_utc]
            m["high_to_open"] = _pct(window["high"].max(), pre_close_1m) if not window.empty else None
            m["low_to_open"] = _pct(window["low"].min(), pre_close_1m) if not window.empty else None
        else:
            t10m_bar = _closest_bar(df_1m, ts + timedelta(minutes=10))
            m["move_10m"] = _pct(t10m_bar["close"], pre_close_1m) if t10m_bar is not None else None
            m["move_to_open"] = m["high_to_open"] = m["low_to_open"] = None
    else:
        m["vol_spike"] = m["move_1m"] = m["move_5m"] = None
        m["move_to_open"] = m["high_to_open"] = m["low_to_open"] = None
        m["move_10m"] = None

    return m


# ── Catalyst classification ───────────────────────────────────────────────────

CATALYST_RULES = [
    ("HALT",             ["halted", "circuit breaker"]),
    ("RESUME",           ["resumes trading", "resume trade"]),
    ("DILUTIVE OFFERING",["offering", "registered direct", "priced at $", "public offering", "prices $"]),
    ("MERGER / S-4",     ["merger", "s-4", "acquisition", "rebrand", "combined company"]),
    ("FDA ACTION",       ["fda", "clinical hold"]),
    ("ANALYST DOWNGRADE",["downgrades", "lowers price target", "speculative buy", "to neutral"]),
    ("ANALYST UPGRADE",  ["upgrades", "raises price target", "outperform"]),
    ("EARNINGS BEAT",    ["beats estimate", "better-than-expected", "top estimate", "beat $", "surges after"]),
    ("EARNINGS MISS",    ["misses estimate", "worse-than", "below estimate"]),
    ("REVENUE GROWTH",   ["record revenue", "revenue surges", "revenue growth"]),
    ("PRODUCT LAUNCH",   ["launch", "releases platform", "new technology", "selected by"]),
    ("EARNINGS REPORT",  ["reports results", "quarterly"]),
]


def classify_catalyst(headline: str) -> str:
    hl = headline.lower()
    for cat_type, keywords in CATALYST_RULES:
        if any(kw in hl for kw in keywords):
            return cat_type
    return "EARNINGS REPORT"


def is_halt_event(headline: str) -> bool:
    hl = headline.lower()
    return any(kw in hl for kw in ["halted", "circuit breaker", "resumes trading", "resume trade"])


# ── Signal assignment ─────────────────────────────────────────────────────────

def assign_signal(cat_type: str, m: dict, headline: str, df_1m_empty: bool) -> str:
    if df_1m_empty and m.get("move_10s") is None:
        return "NO DATA"

    move_1m = m.get("move_1m")
    vol_spike = m.get("vol_spike") or 0.0
    hl = headline.lower()

    if cat_type == "HALT":
        direction = "↑" if "upside" in hl or "up" in hl else "↓"
        pct_match = re.search(r"(\d+\.?\d*)\s*%", headline)
        pct_str = f"({'+' if direction == '↑' else '-'}{pct_match.group(1)}%)" if pct_match else ""
        return f"HALTED {direction} {pct_str}".strip()

    if cat_type == "RESUME":
        return "RESUME"

    if cat_type == "EARNINGS BEAT":
        if move_1m is not None and move_1m >= 2.0 and vol_spike >= 2.0:
            return "LONG ✓"

    if cat_type == "EARNINGS MISS":
        if move_1m is not None and move_1m <= -2.0:
            return "AVOID"

    if cat_type == "DILUTIVE OFFERING":
        if move_1m is not None and move_1m <= -3.0:
            return "AVOID (dilution)"
        if move_1m is not None and move_1m > 0:
            return "FADE SHORT"

    if cat_type == "MERGER / S-4":
        if move_1m is not None and move_1m >= 3.0 and vol_spike >= 3.0:
            return "LONG ✓ (speculative)"

    if cat_type == "FDA ACTION":
        return "AVOID"

    if cat_type == "ANALYST DOWNGRADE":
        return "AVOID"

    if cat_type == "ANALYST UPGRADE":
        if move_1m is not None and move_1m >= 1.0:
            return "LONG ✓"

    if cat_type == "REVENUE GROWTH":
        if move_1m is not None and move_1m >= 3.0:
            return "LONG ✓"

    if cat_type == "PRODUCT LAUNCH":
        if move_1m is not None and move_1m >= 3.0:
            return "LONG ✓ (speculative)"

    return "WATCH"


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt(val: Optional[float], decimals: int = 1) -> str:
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.{decimals}f}%"


def _fmt_vol(val: Optional[float]) -> str:
    if val is None:
        return "—"
    return f"{val:.1f}x"


def _fmt_time(dt_utc) -> str:
    dt_et = dt_utc.astimezone(ET)
    return dt_et.strftime("%I:%M %p").lstrip("0")


def _abbrev_cat(cat_type: str, is_premarket: bool) -> str:
    tag = "[PM]" if is_premarket else "[IN]"
    short = {
        "DILUTIVE OFFERING": "DILUTIVE OFFER",
        "ANALYST DOWNGRADE": "ANALYST DOWN",
        "ANALYST UPGRADE":   "ANALYST UP",
        "EARNINGS BEAT":     "EARNINGS BEAT",
        "EARNINGS MISS":     "EARNINGS MISS",
        "EARNINGS REPORT":   "EARNINGS RPT",
        "REVENUE GROWTH":    "REV GROWTH",
        "PRODUCT LAUNCH":    "PRODUCT LAUNCH",
        "MERGER / S-4":      "MERGER/S-4",
        "FDA ACTION":        "FDA ACTION",
        "HALT":              "HALT",
        "RESUME":            "RESUME",
    }.get(cat_type, cat_type)
    return f"{short} {tag}"


# ── Main analysis loop ────────────────────────────────────────────────────────

def get_analysis_data():
    import math
    events = parse_events(INPUT_FILE)
    results = []
    no_data_events = []

    for idx, event in enumerate(events, 1):
        df_1m, is_premarket = fetch_bars(event)
        m = compute_metrics(event, df_1m, is_premarket)
        cat_type = classify_catalyst(event.headline)
        signal = assign_signal(cat_type, m, event.headline, df_1m.empty)

        candles = []
        if not df_1m.empty:
            for _, row_data in df_1m.iterrows():
                candles.append({
                    "timestamp": row_data["timestamp"].isoformat() if hasattr(row_data["timestamp"], "isoformat") else str(row_data["timestamp"]),
                    "open": float(row_data["open"]),
                    "high": float(row_data["high"]),
                    "low": float(row_data["low"]),
                    "close": float(row_data["close"]),
                    "volume": float(row_data["volume"]),
                })

        # Sanitize metrics for JSON serialization
        clean_m = {}
        for k, v in m.items():
            if v is None or (isinstance(v, float) and math.isnan(v)):
                clean_m[k] = None
            else:
                clean_m[k] = v

        row = dict(
            event={
                "dt_utc": event.dt_utc.isoformat(),
                "symbol": event.symbol,
                "headline": event.headline,
                "source": event.source,
                "date_label": event.date_label,
            },
            is_premarket=is_premarket,
            cat_type=cat_type,
            signal=signal,
            candles=candles,
            **clean_m,
        )

        if signal == "NO DATA":
            no_data_events.append(row)
        else:
            results.append(row)

    return {"results": results, "no_data_events": no_data_events}

def run():
    print("\nParsing news events…")
    events = parse_events(INPUT_FILE)
    print(f"  {len(events)} events found.\n")

    results = []
    no_data_events = []

    for idx, event in enumerate(events, 1):
        print(f"  [{idx}/{len(events)}] {event.symbol:6s} {_fmt_time(event.dt_utc)}", end="  ", flush=True)
        df_1m, is_premarket = fetch_bars(event)
        m = compute_metrics(event, df_1m, is_premarket)
        cat_type = classify_catalyst(event.headline)
        signal = assign_signal(cat_type, m, event.headline, df_1m.empty)
        print(f"{cat_type:20s}  {signal}")

        row = dict(
            event=event,
            is_premarket=is_premarket,
            cat_type=cat_type,
            signal=signal,
            **m,
        )

        if signal == "NO DATA":
            no_data_events.append(row)
        else:
            results.append(row)

    _print_table(results, no_data_events)
    _print_pattern_summary(results)
    _print_scanner_rules(results)


# ── Table output ──────────────────────────────────────────────────────────────

def _print_table(results: list[dict], no_data: list[dict]):
    W = 130
    print()
    print("═" * W)
    print("BENZINGA CATALYST ANALYSIS   May 11–15, 2026")
    print("═" * W)

    # group by date_label (preserves insertion order = most-recent-first from file)
    from collections import defaultdict
    by_day: dict[str, list[dict]] = defaultdict(list)
    order: list[str] = []
    for row in results:
        label = row["event"].date_label
        if label not in by_day:
            order.append(label)
        by_day[label].append(row)

    hdr = (
        f"{'TIME(ET)':<10} {'TICKER':<7} {'CATALYST TYPE':<22}"
        f"{'T+10s':>7} {'T+30s':>7} {'T+1m':>7} {'T+5m':>7}"
        f"{'TO OPEN':>9} {'HIGH':>8} {'LOW':>8}"
        f"{'VOL':>7}  {'SIGNAL'}"
    )

    for label in order:
        print()
        print(f"── {label} " + "─" * (W - len(label) - 4))
        print(" " * 40 + "←── 10-sec ──→  ←── 1-min ──────────────────────────────→")
        print(hdr)
        for row in by_day[label]:
            e = row["event"]
            print(
                f"{_fmt_time(e.dt_utc):<10} {e.symbol:<7} "
                f"{_abbrev_cat(row['cat_type'], row['is_premarket']):<22}"
                f"{_fmt(row.get('move_10s')):>7} "
                f"{_fmt(row.get('move_30s')):>7} "
                f"{_fmt(row.get('move_1m') if row.get('move_1m') is not None else row.get('move_1m_fine')):>7} "
                f"{_fmt(row.get('move_5m')):>7}"
                f"{_fmt(row.get('move_to_open')):>9} "
                f"{_fmt(row.get('high_to_open')):>8} "
                f"{_fmt(row.get('low_to_open')):>8}"
                f"{_fmt_vol(row.get('vol_spike')):>7}  "
                f"{row['signal']}"
            )

    if no_data:
        print()
        print(f"── [NO DATA] " + "─" * (W - 14))
        for row in no_data:
            e = row["event"]
            print(f"  {e.symbol:<7} {_fmt_time(e.dt_utc)}  {e.headline[:80]}")


# ── Pattern summary ───────────────────────────────────────────────────────────

def _print_pattern_summary(results: list[dict]):
    from collections import defaultdict
    import statistics

    W = 130
    print()
    print("═" * W)
    print("PATTERN SUMMARY")
    print("═" * W)

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for row in results:
        by_cat[row["cat_type"]].append(row)

    def _avg(rows, key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return statistics.mean(vals) if vals else None

    for cat_type, rows in sorted(by_cat.items(), key=lambda x: -len(x[1])):
        n = len(rows)
        avg_10s = _avg(rows, "move_10s")
        avg_1m = _avg(rows, "move_1m")
        avg_5m = _avg(rows, "move_5m")
        avg_vol = _avg(rows, "vol_spike")
        print(
            f"{cat_type:<22} N={n:<3}"
            f"  avg T+10s: {_fmt(avg_10s):<9}"
            f"  avg T+1m: {_fmt(avg_1m):<9}"
            f"  avg T+5m: {_fmt(avg_5m):<9}"
            f"  avg vol: {_fmt_vol(avg_vol)}"
        )


# ── Scanner rule suggestions ──────────────────────────────────────────────────

def _print_scanner_rules(results: list[dict]):
    from collections import defaultdict
    import statistics

    W = 130
    print()
    print("═" * W)
    print("SUGGESTED SCANNER RULES")
    print("═" * W)

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for row in results:
        by_cat[row["cat_type"]].append(row)

    def _avg5(rows):
        vals = [r["move_5m"] for r in rows if r.get("move_5m") is not None]
        return statistics.mean(vals) if vals else None

    # Negative catalysts → Filter OUT
    for cat in ("DILUTIVE OFFERING", "ANALYST DOWNGRADE", "FDA ACTION", "EARNINGS MISS"):
        if cat in by_cat:
            rows = by_cat[cat]
            avg5 = _avg5(rows)
            print(f"→ Filter OUT:  {cat:<22} (avg T+5: {_fmt(avg5)}, seen {len(rows)} times)")

    # Positive catalysts → WATCHLIST
    for cat in ("EARNINGS BEAT", "REVENUE GROWTH", "MERGER / S-4", "ANALYST UPGRADE", "PRODUCT LAUNCH"):
        if cat in by_cat:
            rows = by_cat[cat]
            avg5 = _avg5(rows)
            avg_vol = [r["vol_spike"] for r in rows if r.get("vol_spike") is not None]
            vol_str = f" with vol_spike ≥ {statistics.mean(avg_vol):.1f}x" if avg_vol else ""
            print(f"→ WATCHLIST:   {cat:<22}{vol_str} (avg T+5 move: {_fmt(avg5)})")

    print()


if __name__ == "__main__":
    run()
