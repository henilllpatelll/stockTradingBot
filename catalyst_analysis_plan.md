# Benzinga Pro Catalyst Analyzer — Build Plan

## Overview
A standalone `catalyst_analyzer.py` script that reads a week of Benzinga Pro news from `ticker_symbols_benpro.md`, fetches Alpaca SIP minute bars around each catalyst timestamp, and outputs a formatted analysis table showing immediate price reactions, volume spikes, catalyst types, and TRADEABLE/AVOID signals.

**Zero changes to existing project files.** Only `catalyst_analyzer.py` is created.

---

## Input
- `ticker_symbols_benpro.md` — Benzinga Pro news feed (Mon May 11 – Fri May 15, 2026, ~90 events, ~31 tickers)

## Output
- Console table grouped by trading day (most recent first)
- Pattern summary section (avg T+1/T+5 move + vol spike per catalyst type)
- Suggested scanner rules based on observed patterns

---

## Step 1 — Parse `ticker_symbols_benpro.md`

**Format of each news block:**
```
[HH:MM:SSAM/PM]
[TICKER [+N]]
[headline text]
[source: BZ Wire / PRN / GLN / BW]
```
Date context line (e.g., `Friday May 15, 2026`) resets the current date.

**Parsing logic:**
- Match date headers with: `r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) \w+ \d+, \d{4}$"`
- Strip `+N` count suffix from ticker lines (e.g., `LNZA +1` → `LNZA`)
- Convert each `(date + time)` to UTC-aware datetime via `pytz.timezone("America/New_York")`

**Skip these generic multi-ticker entries** (sector sweeps, not single-stock catalysts):
- ARAI +45, ADUR +71, ACOG +165, ABEO +151, ABOS +146, ARXS +10, APA +20, INHD +2
- Any entry where the headline is "Earnings Scheduled For..."

**Output:** List of `NewsEvent(dt_utc, symbol, headline, source, date_label)`

---

## Step 2 — Fetch Alpaca SIP Bars (1-Min + 10-Sec)

**Do NOT modify `alpaca_client.py`.** Instead, directly use `alpaca-py` inside the new script:

```python
import os
from dotenv import load_dotenv
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

load_dotenv()
data_client = StockHistoricalDataClient(
    api_key=os.getenv("ALPACA_API_KEY"),
    secret_key=os.getenv("ALPACA_SECRET_KEY"),
)

TF_1MIN = TimeFrame.Minute
TF_10SEC = TimeFrame(10, TimeFrameUnit.Second)
```

For each `NewsEvent`, fetch **two** bar windows:

```python
import pytz
ET = pytz.timezone("America/New_York")

def market_open_utc(event_dt_utc):
    """Return 9:30 AM ET on the same calendar date as the event."""
    local = event_dt_utc.astimezone(ET)
    open_et = ET.localize(local.replace(hour=9, minute=30, second=0, microsecond=0))
    return open_et.astimezone(pytz.utc)

is_premarket = event.dt_utc < market_open_utc(event.dt_utc)

# 1-minute bars:
#   Premarket  → news time to 9:30 AM ET (full premarket development)
#   Intraday   → news time to news time + 10 min
end_1m = market_open_utc(event.dt_utc) if is_premarket else event.dt_utc + timedelta(minutes=10)

req_1m = StockBarsRequest(
    symbol_or_symbols=symbol,
    timeframe=TF_1MIN,
    start=event.dt_utc - timedelta(minutes=5),
    end=end_1m,
    feed="sip",
)
df_1m = data_client.get_stock_bars(req_1m).df

# 10-second bars: always a tight 2 min before → 5 min after window (immediate reaction only)
req_10s = StockBarsRequest(
    symbol_or_symbols=symbol,
    timeframe=TF_10SEC,
    start=event.dt_utc - timedelta(minutes=2),
    end=event.dt_utc + timedelta(minutes=5),
    feed="sip",
)
df_10s = data_client.get_stock_bars(req_10s).df
```

Add `time.sleep(0.2)` between events (~90 events × 2 calls × 0.2s ≈ 36 sec total).

---

## Step 3 — Compute Metrics

**From 10-second bars (`df_10s`) — immediate reaction:**
```
pre_10s      = last bar where timestamp <= event.dt_utc
t10s_bar     = first bar where timestamp > event.dt_utc         (T+10 sec)
t30s_bar     = bar closest to event.dt_utc + 30 sec             (T+30 sec)
t1m_10s      = bar closest to event.dt_utc + 60 sec             (T+1 min, fine-grain)

move_10s     = (t10s_close - pre_10s_close) / pre_10s_close * 100
move_30s     = (t30s_close - pre_10s_close) / pre_10s_close * 100
move_1m_fine = (t1m_10s_close - pre_10s_close) / pre_10s_close * 100
```
The 10-sec bars reveal whether the first-tick reaction is a spike-and-fade or a sustained move.

**From 1-minute bars (`df_1m`) — full premarket development:**
```
pre_bar   = last bar where timestamp <= event.dt_utc
t1m_bar   = first bar where timestamp > event.dt_utc            (T+1 min)
t5m_bar   = bar closest to event.dt_utc + 5 min                 (T+5 min)
vol_spike = t1m_volume / mean(last 5 pre_bars volumes)          (None if denom=0)

move_1m   = (t1m_close  - pre_close) / pre_close * 100
move_5m   = (t5m_close  - pre_close) / pre_close * 100

# Premarket events only (is_premarket = True):
open_bar     = bar at or just before 9:30 AM ET
move_to_open = (open_bar_close - pre_close) / pre_close * 100
high_to_open = max(df_1m[news_time : 9:30].high)               (peak reached)
low_to_open  = min(df_1m[news_time : 9:30].low)                (trough reached)

# Intraday events (is_premarket = False):
t10m_bar     = bar closest to event.dt_utc + 10 min
move_10m     = (t10m_bar_close - pre_close) / pre_close * 100
move_to_open = N/A
```


**Circuit-breaker entries** (headlines containing "halted", "circuit breaker", "resumes trading", "resume trade"):
- Flag `is_halt = True`
- Still compute metrics where data is available
- Note the percentage in the SIGNAL column (e.g., "HALTED ↑ (+148%)")

---

## Step 4 — Classify Catalyst Type

Priority-ordered keyword scan of lowercased headline (first match wins):

| # | Catalyst Type | Match Keywords |
|---|---|---|
| 1 | `HALT` | halted, circuit breaker |
| 2 | `RESUME` | resumes trading, resume trade |
| 3 | `DILUTIVE OFFERING` | offering, registered direct, priced at $, public offering, prices $ |
| 4 | `MERGER / S-4` | merger, s-4, acquisition, rebrand, combined company |
| 5 | `FDA ACTION` | fda, clinical hold |
| 6 | `ANALYST DOWNGRADE` | downgrades, lowers price target, speculative buy, to neutral |
| 7 | `ANALYST UPGRADE` | upgrades, raises price target, outperform |
| 8 | `EARNINGS BEAT` | beats estimate, better-than-expected, top estimate, beat $, surges after |
| 9 | `EARNINGS MISS` | misses estimate, worse-than, below estimate |
| 10 | `REVENUE GROWTH` | record revenue, revenue surges, revenue growth |
| 11 | `PRODUCT LAUNCH` | launch, releases platform, new technology, selected by |
| 12 | `EARNINGS REPORT` | reports results, quarterly (fallback) |

---

## Step 5 — Assign TRADEABLE Signal

| Condition | Signal |
|---|---|
| EARNINGS BEAT + move_1m ≥ +2% + vol_spike ≥ 2× | `LONG ✓` |
| EARNINGS MISS + move_1m ≤ −2% | `AVOID` |
| DILUTIVE OFFERING + move_1m ≤ −3% | `AVOID (dilution)` |
| DILUTIVE OFFERING + move_1m > 0 | `FADE SHORT` |
| MERGER/S-4 + move_1m ≥ +3% + vol_spike ≥ 3× | `LONG ✓ (speculative)` |
| FDA ACTION | `AVOID` |
| ANALYST DOWNGRADE | `AVOID` |
| ANALYST UPGRADE + move_1m ≥ +1% | `LONG ✓` |
| REVENUE GROWTH + move_1m ≥ +3% | `LONG ✓` |
| PRODUCT LAUNCH + move_1m ≥ +3% | `LONG ✓ (speculative)` |
| HALT event | `HALTED ↑ / HALTED ↓` |
| RESUME event | `RESUME` |
| Empty DataFrame returned | `NO DATA` |
| else | `WATCH` |

---

## Step 6 — Output Format

```
════════════════════════════════════════════════════════════════════════
BENZINGA CATALYST ANALYSIS   May 11–15, 2026
════════════════════════════════════════════════════════════════════════

── Friday May 15, 2026 ──────────────────────────────────────────────────
                                        ←── 10-sec ──→  ←── 1-min ──────────────────────────────→
TIME(ET)   TICKER  CATALYST TYPE        T+10s   T+30s   T+1m    T+5m    TO OPEN   HIGH    LOW     VOL    SIGNAL
10:22 AM   PIII    RESUME [INTRADAY]    +X.X%   +X.X%   +X.X%   +X.X%   —         —       —       X.Xx   RESUME
10:11 AM   PIII    HALT   [INTRADAY]     —       —       —       —       —         —       —        —     HALTED ↑ (+148%)
10:07 AM   QUCY    PRODUCT LAUNCH [IN]  +X.X%   +X.X%   +X.X%   +X.X%   —         —       —       X.Xx   LONG ✓ (speculative)
09:27 AM   AARD    FDA ACTION [PM]      -X.X%   -X.X%   -X.X%   -X.X%   -X.X%     -X.X%   -X.X%   X.Xx   AVOID
08:23 AM   WYY     EARNINGS BEAT [PM]   +X.X%   +X.X%   +X.X%   +X.X%   +X.X%     +X.X%   +X.X%   X.Xx   LONG ✓
07:32 AM   QTI     DILUTIVE OFFER [PM]  -X.X%   -X.X%   -X.X%   -X.X%   -X.X%     -X.X%   -X.X%   X.Xx   AVOID (dilution)
...
...

── Thursday May 14, 2026 ────────────────────────────────────────────────
...

── [NO DATA] ────────────────────────────────────────────────────────────
(tickers where Alpaca returned no bars for that window)

════════════════════════════════════════════════════════════════════════
PATTERN SUMMARY
════════════════════════════════════════════════════════════════════════
EARNINGS BEAT      N=X  avg T+10s: +X.X%  avg T+1m: +X.X%  avg T+5m: +X.X%  avg vol: X.Xx
DILUTIVE OFFERING  N=X  avg T+10s: -X.X%  avg T+1m: -X.X%  avg T+5m: -X.X%  avg vol: X.Xx
ANALYST DOWNGRADE  N=X  avg T+10s: -X.X%  avg T+1m: -X.X%  avg T+5m: -X.X%  avg vol: X.Xx
MERGER / S-4       N=X  avg T+10s: +X.X%  avg T+1m: +X.X%  avg T+5m: +X.X%  avg vol: X.Xx
...

════════════════════════════════════════════════════════════════════════
SUGGESTED SCANNER RULES
════════════════════════════════════════════════════════════════════════
→ Filter OUT:  DILUTIVE OFFERING  (avg T+5: -X.X%, seen N times)
→ Filter OUT:  ANALYST DOWNGRADE  (avg T+5: -X.X%, seen N times)
→ WATCHLIST:   EARNINGS BEAT with vol_spike ≥ Xx (avg T+5 move: +X.X%)
→ WATCHLIST:   MERGER/S-4 premarket filings (speculative, avg: +X.X%)
```

---

## Key Multi-Day Storylines to Verify

| Ticker | Story | Expected Signal |
|---|---|---|
| PIII | Earnings beat May 14 AH → 42% surge → 4 halts May 15 | LONG ✓ → HALTED ↑ ×4 |
| GAMB | Guidance cut May 14 AH → analyst downgrade May 15 | AVOID on both days |
| AARD | FDA clinical hold + analyst downgrade same morning | AVOID ×2 |
| LNZA | Q1 EPS beat May 14 → $20M offering May 15 | LONG ✓ → AVOID (dilution) |
| LESL | Q2 beat May 13 AH → +26% AH → +130% May 14 intraday | LONG ✓ chain |
| BRAG | Earnings beat + acquisition May 14 AM | LONG ✓ |

---

## Files

| File | Action |
|---|---|
| `catalyst_analyzer.py` | Create (standalone, ~200 lines) |
| `ticker_symbols_benpro.md` | Read-only input |
| All other project files | Untouched |

---

## Dependencies (already in requirements.txt)
- `alpaca-py>=0.28.0`
- `pandas>=2.0.0`
- `python-dotenv>=1.0.0`
- `pytz` (included with pandas)

## Run
```
python catalyst_analyzer.py
```
