# Trading Bot — Full Strategy Upgrade Build Spec

## What This Is
A complete upgrade spec for `c:\Users\Henil\projects\stock-trading`. This rewrites the bot around a disciplined premarket momentum playbook:

- Automated scanner from 4:00 AM ET, refresh every 30 seconds
- Catalyst verification via Alpaca News API (same SDK/keys, no new credentials)
- Filters: gap > 4%, price $1–$20, float < 20M, RVOL ≥ 5x, already up ≥ 5%, visible catalyst
- Named setup detection: premarket high break, flag break, pivot break, red-to-green, 1-min ORB, 5-min ORB, first pullback bull flag, flat-top breakout
- Profit taking: sell 50% at 2R, move stop to breakeven, exit remainder on red-candle or extension-bar
- Stop: below first pullback / ORB low / flag bottom — ATR-based, setup-aware
- Slippage: ask +$0.02 entry, bid (no haircut) exit, 15s fill timeout, skip if spread > 2%
- Trading window: 7:00 AM – 11:00 AM ET (6:00 AM – 10:00 AM CT)

**Do not change `paper_only = True`. All trading is paper-only.**

---

## What to Ignore / Remove from the Current Code

- The fixed T2 take-profit level (next whole/half-dollar) — **remove entirely**
- The T3 9-EMA close exit — **remove entirely, replaced by red-candle / extension-bar logic**
- `_limit_sell` — **rename to `_exit_sell` and reroute through `place_exit_limit_order`**
- `finnhub_api_key` — **never add this; news uses Alpaca**
- Entry limit `ask + $0.05` — **replace with `ask + $0.02`**
- Fill timeout of 30 seconds — **replace with 15 seconds**

---

## Implementation Order

1. `config.py`
2. `alpaca_client.py`
3. `trade_logger.py`
4. `risk_manager.py`
5. `strategy.py`
6. `scanner.py` (new file)
7. `run_bot.py`
8. `requirements.txt`

---

## Step 1 — `config.py`

### `FilterConfig` changes
```python
min_price: float = 1.0           # was 2.0
max_price: float = 20.0
max_float_shares: float = 20_000_000
max_market_cap: float = 2_000_000_000
min_change_pct: float = 5.0      # live entry gate (already up ≥ 5%)
scanner_min_gap_pct: float = 4.0 # new — scanner display threshold
min_rvol: float = 5.0            # was 1.0
```

### `RiskConfig` additions (append — keep all existing fields)
```python
min_rr_ratio: float = 2.0             # T1 at 2:1 — minimum R/R
t1_partial_pct: float = 0.50          # sell 50% at T1
extension_bar_atr_mult: float = 2.0   # candle range > 2× ATR = extension/climax bar
stall_bars: int = 3                   # bars with no progress before bailout
stall_progress_threshold: float = 0.10
momentum_fail_vol_ratio: float = 0.50 # volume < 50% of entry bar = momentum fail
momentum_fail_bars: int = 2
entry_fill_timeout_seconds: int = 15  # was 30
```

### New `ScannerConfig` dataclass (add before `Config`)
```python
@dataclass
class ScannerConfig:
    scan_start_hour_et: int = 4
    scan_end_hour_et: int = 9
    scan_end_minute_et: int = 30
    refresh_interval_seconds: int = 30
    alert_hours_et: list = field(default_factory=lambda: [7, 8, 9])
    news_lookback_hours: int = 24
    max_candidates: int = 20
```
No API key field — Alpaca news uses existing `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`.

### `Config` additions
```python
scanner: ScannerConfig = field(default_factory=ScannerConfig)
trading_start_hour_et: int = 7    # 7:00 AM ET = 6:00 AM CT
trading_end_hour_et: int = 11     # 11:00 AM ET = 10:00 AM CT
```

---

## Step 2 — `alpaca_client.py`

Add five methods. Do not change any existing method signatures.

```python
def get_quote_with_spread(self, symbol: str) -> dict:
    """
    Returns:
        ask, bid, mid, spread_cents, spread_pct,
        wide_spread: bool  (True if spread_pct > 2.0 — hidden seller / halt risk)
    Uses get_latest_quote() internally.
    """

def place_entry_limit_order(self, symbol: str, qty: int, ask: float):
    """
    Entry limit = round(ask + 0.02, 2).
    Tighter than old +$0.05 — better slippage control.
    Calls existing place_limit_order(symbol, qty, OrderSide.BUY, limit).
    """

def place_exit_limit_order(self, symbol: str, qty: int, bid: float):
    """
    Exit limit = round(bid, 2). No haircut on exits.
    Calls existing place_limit_order(symbol, qty, OrderSide.SELL, limit).
    """

def get_premarket_bars(self, symbol: str, date=None) -> pd.DataFrame:
    """
    1-minute bars from 4:00 AM ET to 9:29 AM ET for date (default today).
    Convert ET window to UTC for the Alpaca StockBarsRequest.
    Use pytz.timezone("America/New_York") for the conversion.
    """

def get_news(self, symbol: str, lookback_hours: int = 24) -> list[str]:
    """
    Fetch recent news headlines via Alpaca News API (Benzinga-sourced).

    In __init__, add alongside self._trading and self._data:
        from alpaca.data.historical import NewsClient
        self._news = NewsClient(
            api_key=config.alpaca.api_key,
            secret_key=config.alpaca.secret_key,
        )

    Call:
        from alpaca.data.requests import NewsRequest
        start = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        req = NewsRequest(symbols=[symbol], start=start, limit=10)
        articles = self._news.get_news(req)
        return [a.headline for a in articles]

    Return [] on any exception — never raise from this method.
    If alpaca-py version does not expose NewsClient, upgrade alpaca-py to latest.
    """
```

---

## Step 3 — `trade_logger.py`

1. Append to `_FIELDS` (end — backward compatible):
   ```python
   "setup_name",
   "catalyst_headline",
   ```

2. Add to `TradeRecord`:
   ```python
   setup_name: str = ""
   catalyst_headline: str = ""
   ```

3. In `log_exit`, change `csv.DictReader(fh)` to `csv.DictReader(fh, restval="")` so old rows without new columns don't crash.

4. In `print_summary`, add a `Setup` column to the per-trade table row (alongside `Reason`).

---

## Step 4 — `risk_manager.py`

### `TakeProfitTargets` — simplify to one level
```python
@dataclass
class TakeProfitTargets:
    t1: float   # entry + 2 × risk — sell 50% here, then move stop to breakeven
    # No T2. Remainder managed with red-candle / extension-bar logic after T1.
```

### `calculate_targets` — T1 at 2:1, nothing else
```python
def calculate_targets(self, entry_price: float, stop_price: float) -> TakeProfitTargets:
    risk = entry_price - stop_price
    t1 = round(entry_price + self._cfg.min_rr_ratio * risk, 4)  # 2× risk
    return TakeProfitTargets(t1=t1)
```

### `calculate_position_size` — ATR-aware stop
```python
def calculate_position_size(
    self, account_equity: float, entry_price: float,
    atr: float | None = None, setup_stop_ref: float | None = None
) -> PositionSizing:
    """
    Stop priority:
      1. setup_stop_ref if provided (ORB low, flag bottom, pullback low)
      2. entry - max(initial_stop_offset, atr * atr_multiplier) if atr provided
      3. entry - initial_stop_offset as fallback
    """
    if setup_stop_ref is not None:
        initial_stop = setup_stop_ref
    elif atr is not None:
        offset = max(self._cfg.initial_stop_offset, atr * self._cfg.atr_multiplier)
        initial_stop = entry_price - offset
    else:
        initial_stop = entry_price - self._cfg.initial_stop_offset

    shares = max(1, int(self._cfg.position_size_dollars / entry_price))
    risk_amount = shares * (entry_price - initial_stop)
    return PositionSizing(
        shares=shares,
        entry_price=entry_price,
        initial_stop=round(initial_stop, 4),
        risk_amount=round(risk_amount, 2),
        position_value=round(shares * entry_price, 2),
    )
```

### New method `is_red_candle`
```python
def is_red_candle(self, bar: pd.Series) -> bool:
    """True if the completed candle closed below its open (bearish candle)."""
    return float(bar["close"]) < float(bar["open"])
```

### New method `is_extension_bar`
```python
def is_extension_bar(self, bar: pd.Series, atr: float) -> bool:
    """
    True if candle range > atr * extension_bar_atr_mult.
    Signals a climactic / parabolic move — take remaining profits immediately.
    """
    candle_range = float(bar["high"]) - float(bar["low"])
    return candle_range > atr * self._cfg.extension_bar_atr_mult
```

### New method `is_stall`
```python
def is_stall(
    self, bars_since_entry: pd.DataFrame, entry_price: float, t1_price: float
) -> bool:
    """
    True if price has not advanced >= stall_progress_threshold × (t1 - entry)
    within the last stall_bars bars. Triggers fast manual bailout.
    """
    required = self._cfg.stall_progress_threshold * (t1_price - entry_price)
    best_advance = float(bars_since_entry["high"].max()) - entry_price
    return best_advance < required
```

### New method `is_momentum_fail`
```python
def is_momentum_fail(self, bars: pd.DataFrame, entry_bar_volume: float) -> bool:
    """
    True if the last momentum_fail_bars consecutive bars ALL have volume
    below entry_bar_volume * momentum_fail_vol_ratio.
    Immediate exit — momentum gone.
    """
    threshold = entry_bar_volume * self._cfg.momentum_fail_vol_ratio
    recent = bars.tail(self._cfg.momentum_fail_bars)
    return (
        len(recent) >= self._cfg.momentum_fail_bars
        and bool((recent["volume"] < threshold).all())
    )
```

---

## Step 5 — `strategy.py`

### New `PremkLevels` dataclass
```python
@dataclass
class PremkLevels:
    premarket_high: float
    premarket_low: float
    pivots: list   # each: {"type": "high"|"low", "price": float, "bar_index": int}
    flags: list    # each: {"top": float, "bottom": float, "start_index": int, "end_index": int}
```

### New `mark_premarket_levels(bars: pd.DataFrame) -> PremkLevels`
- `premarket_high = bars["high"].max()`
- `premarket_low = bars["low"].min()`
- **Pivot highs**: bar i is a pivot high if `high[i] > high[i-1]` AND `high[i] > high[i+1]`. Mirror for pivot lows. Skip first and last bar.
- **Flag zones**: slide a 4-bar window; if `(high.max() - low.min()) / low.min() < 0.005` (< 0.5% range), mark as flag. `top = window high.max()`, `bottom = window low.min()`. Flag ends at first bar where `close > top * 1.005`.

### Setup detector functions
Each returns `tuple[bool, str, float, float | None]` = `(triggered, setup_name, trigger_price, stop_ref)`.

`trigger_price` = the level the price just broke above (entry reference).
`stop_ref` = suggested stop price for this setup (below the setup's base). `None` if not determinable.

```python
def detect_premarket_high_break(bars, pm_high) -> tuple:
    """
    Triggered: last close > pm_high.
    trigger = pm_high.
    stop_ref = None (use ATR-based stop — first pullback low after entry).
    """

def detect_pm_flag_break(bars, flags) -> tuple:
    """
    Triggered: last close > most recent flag's top.
    trigger = flag top.
    stop_ref = flag bottom (below the flag base).
    """

def detect_pm_pivot_break(bars, pivots) -> tuple:
    """
    Triggered: last close > last pivot high price.
    trigger = last pivot high.
    stop_ref = last pivot low price if available, else None.
    """

def detect_red_to_green(bars, prev_close) -> tuple:
    """
    Triggered: prior bar close < prev_close AND current bar close >= prev_close.
    trigger = prev_close.
    stop_ref = prev_close - small_buffer (e.g., prev_close * 0.995).
    """

def detect_1min_orb(bars, session_open_time) -> tuple:
    """
    Identify the single 1-min bar starting at session_open_time (9:30 AM ET).
    Triggered: last close > that bar's high.
    trigger = orb_high.
    stop_ref = orb_low (low of the opening range bar).
    """

def detect_5min_orb(bars, session_open_time) -> tuple:
    """
    Bars from session_open_time to session_open_time + 5 min.
    orb_high = max(high) over those bars. orb_low = min(low) over those bars.
    Triggered: last close > orb_high.
    trigger = orb_high.
    stop_ref = orb_low.
    """

def detect_first_pullback_bull_flag(bars) -> tuple:
    """
    Pattern: >=3% up move in <=5 bars, then consolidation <0.5% range for >=3 bars,
    then close above consolidation top.
    trigger = consolidation top.
    stop_ref = consolidation bottom.
    """

def detect_flat_top_breakout(bars) -> tuple:
    """
    Pattern: >=2 prior bars with high within $0.05 of same resistance level,
    then close above that level.
    trigger = flat top price.
    stop_ref = None (use ATR-based stop).
    """
```

### `run_setup_detectors` orchestrator
```python
def run_setup_detectors(
    bars, pm_levels, prev_close, session_open_time=None
) -> list:
    """Calls all 8 detectors. Returns list of (triggered, name, trigger_price, stop_ref)."""
```

### `EntrySignal` — add fields
```python
setup_name: str = ""
catalyst_headline: str = ""
setup_stop_ref: float | None = None   # setup-specific stop reference price
```

### `check_entry_signal` — after existing checks pass
Call `run_setup_detectors`, set `signal.setup_name` and `signal.setup_stop_ref` from the first triggered setup. Default `setup_name = "vol_accel_only"` and `setup_stop_ref = None` if none trigger.

---

## Step 6 — `scanner.py` (new file)

### Timezone utilities (module-level)
```python
import pytz

_ET = pytz.timezone("America/New_York")
_CT = pytz.timezone("America/Chicago")

def _et_now() -> datetime:
    """Current time as timezone-aware datetime in US/Eastern."""

def _ct_now() -> datetime:
    """Current time as timezone-aware datetime in US/Central."""

def is_scanner_hours(cfg) -> bool:
    """True between 4:00 AM ET and 9:30 AM ET."""

def is_trading_hours(cfg) -> bool:
    """True between cfg.trading_start_hour_et and cfg.trading_end_hour_et ET.
    7:00 AM – 11:00 AM ET = 6:00 AM – 10:00 AM CT."""

def is_alert_window(cfg) -> tuple:
    """(True, hour) if within 1 minute of a 7, 8, or 9 AM ET alert window. Else (False, -1).
    These windows align with major news release times."""

def seconds_until_scanner_open(cfg) -> float:
    """Seconds until 4:00 AM ET — today if not yet reached, tomorrow if already past."""
```

### `GapperCandidate` dataclass
```python
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
```

### `CatalystChecker` class
```python
class CatalystChecker:
    def __init__(self, config, client) -> None:
        # client = AlpacaClient — news uses the same connection, no extra auth
        self._cfg = config
        self._client = client

    def fetch_headlines(self, symbol: str, lookback_hours: int) -> list:
        """
        Calls self._client.get_news(symbol, lookback_hours).
        Returns list of headline strings.
        Returns [] on any error — never raises.
        Rule: if you cannot explain why it is moving, don't trade.
        """

    def has_catalyst(self, symbol: str, headlines: list) -> tuple:
        """
        (True, first_headline) if headlines non-empty.
        (False, "") if empty — logs 'no catalyst: SYMBOL — skip'.
        """

    def confirm_with_user(self, symbol: str, headline: str) -> bool:
        """
        Prints:
            [CATALYST] SYMBOL: "<headline>"
            Confirm catalyst? [y/n]:
        Returns True if user types y/Y.
        """
```

### `PremarketScanner` class
```python
class PremarketScanner:
    def __init__(self, config, client) -> None:
        self._config = config
        self._client = client
        self._fundamental_cache: dict = {}  # symbol -> (datetime, dict)
        self._seen_symbols: set = set()

    def _get_fundamentals(self, symbol: str) -> dict:
        """yfinance fetch, cached per symbol for 5 minutes to avoid rate limits."""

    def _check_symbol(self, symbol: str) -> GapperCandidate | None:
        """
        Filter checklist (all must pass):
          gap >= 4%          (scanner_min_gap_pct)
          price $1–$20
          float < 20M shares
          RVOL >= 5x
          already up >= 5%  (min_change_pct)
        Log the specific failure reason for any that don't pass.
        Return None on any failure or data error.
        """

    def scan_once(self, universe: list) -> list:
        """Check all tickers. Return passing GapperCandidates sorted by RVOL descending."""

    def print_ranked_candidates(self, candidates: list) -> None:
        """
        Console table (sorted RVOL desc):
        RANK  SYMBOL  PRICE  GAP%  RVOL  FLOAT(M)  CATALYST
        """

    def run_scan_loop(self, universe: list, catalyst_checker, on_candidate=None) -> None:
        """
        Outer loop:
        1. If outside scanner hours (before 4 AM ET), sleep until open.
        2. Every 30 seconds: scan_once(universe).
        3. For each new candidate not in self._seen_symbols:
             - fetch_headlines() → has_catalyst()
             - If no catalyst: log and skip (do not add to seen — re-check next scan)
             - If catalyst: add to seen, print ranked table
        4. At 7, 8, 9 AM ET windows: print 'Running Up' alert for all active candidates.
        5. Stop loop at 9:30 AM ET.
        6. On Ctrl-C: print final ranked table and exit gracefully.
        """
```

---

## Step 7 — `run_bot.py`

### New CLI arguments
```
--scanner                 Run automated premarket scanner loop (4 AM – 9:30 AM ET)
--universe PATH           Newline-delimited .txt file of tickers for scanner
--no-catalyst-check       Skip Alpaca news catalyst check (testing only)
--no-trading-hours-gate   Allow trade outside 7–11 AM ET (testing only)
```

### New `run_scanner_mode(config, args)` function
```python
def run_scanner_mode(config, args) -> None:
    if not args.universe:
        print("Error: --scanner requires --universe PATH"); return
    universe = [t.strip().upper() for t in Path(args.universe).read_text().splitlines() if t.strip()]
    client = AlpacaClient(config)
    scanner = PremarketScanner(config, client)
    checker = CatalystChecker(config.scanner, client)
    try:
        scanner.run_scan_loop(universe, checker)
    except KeyboardInterrupt:
        pass
```

### `run()` function — full updated flow (in order)

**1. Update signature to accept `args`:**
```python
def run(symbol, config, skip_filters=False, skip_entry_signal=False, args=None) -> None:
```

**2. Trading hours gate (very top, before any API calls):**
```python
from scanner import is_trading_hours
if args and not getattr(args, 'no_trading_hours_gate', False):
    if not is_trading_hours(config):
        log.warning("Outside trading hours (7–11 AM ET / 6–10 AM CT) — no trade.")
        return
```

**3. After filters pass — wide-spread gate:**
```python
quote = client.get_quote_with_spread(symbol)
if quote["wide_spread"]:
    print(f"{symbol}: spread {quote['spread_pct']:.1f}% > 2% — skipping (widened spread / hidden seller risk).")
    return
```

**4. After filters pass — catalyst check:**
```python
catalyst_headline = ""
no_catalyst_check = args and getattr(args, 'no_catalyst_check', False)
if not no_catalyst_check:
    from scanner import CatalystChecker
    checker = CatalystChecker(config.scanner, client)
    headlines = checker.fetch_headlines(symbol, config.scanner.news_lookback_hours)
    has_cat, headline = checker.has_catalyst(symbol, headlines)
    if not has_cat:
        print(f"{symbol}: no catalyst — if you cannot explain why it is moving, don't trade.")
        return
    if not checker.confirm_with_user(symbol, headline):
        print(f"{symbol}: catalyst not confirmed — skipping.")
        return
    catalyst_headline = headline
```

**5. Premarket level marking:**
```python
from strategy import mark_premarket_levels, run_setup_detectors
pm_bars = client.get_premarket_bars(symbol)
pm_levels = mark_premarket_levels(pm_bars)
log.info(
    "%s: pm_high=$%.2f  pm_low=$%.2f  pivots=%d  flags=%d",
    symbol, pm_levels.premarket_high, pm_levels.premarket_low,
    len(pm_levels.pivots), len(pm_levels.flags),
)
```

**6. Setup detection and display:**
```python
import datetime as _dt
et_tz = pytz.timezone("America/New_York")
now_et = _dt.datetime.now(et_tz)
session_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(_dt.timezone.utc)

bars = client.get_intraday_bars(symbol, lookback_hours=6)
setup_results = run_setup_detectors(bars, pm_levels, filter_result.prev_close, session_open)
active = [(name, trig, stop_ref) for ok, name, trig, stop_ref in setup_results if ok]

if active:
    print(f"\n[{symbol}] Active setups:")
    for name, trig, stop_ref in active:
        stop_str = f"  stop_ref=${stop_ref:.2f}" if stop_ref else ""
        print(f"  {name}  trigger=${trig:.2f}{stop_str}")
else:
    print(f"\n[{symbol}] No named setup active — vol-accel only.")
```

**7. ATR computation (before position sizing):**
```python
def _compute_atr(bars) -> float | None:
    if bars is None or len(bars) < 2:
        return None
    pc = bars["close"].shift(1)
    tr = pd.concat([
        bars["high"] - bars["low"],
        (bars["high"] - pc).abs(),
        (bars["low"]  - pc).abs(),
    ], axis=1).max(axis=1)
    val = float(tr.rolling(14, min_periods=1).mean().iloc[-1])
    return val if val > 0 else None

atr = _compute_atr(pm_bars)
setup_stop_ref = active[0][2] if active else None
sizing = risk_mgr.calculate_position_size(
    equity, signal.entry_price, atr=atr, setup_stop_ref=setup_stop_ref
)
```

**8. Entry order — tighter slippage:**
```python
# Wide-spread already checked above; use that same quote
entry_order = client.place_entry_limit_order(symbol, sizing.shares, quote["ask"])
fill_deadline = time.monotonic() + config.risk.entry_fill_timeout_seconds  # 15s
```

**9. `TradeRecord` construction:**
```python
trade = TradeRecord(
    symbol=symbol,
    entry_time=_now_iso(),
    entry_price=signal.entry_price,
    shares=sizing.shares,
    initial_stop=sizing.initial_stop,
    trail_pct=0.0,
    rvol=signal.rvol,
    change_pct=signal.change_pct,
    setup_name=active[0][0] if active else "vol_accel_only",
    catalyst_headline=catalyst_headline,
)
```

**10. Entry banner — update to show 2:1 and new stop logic:**
```python
print(
    f"\n{'═' * 52}\n"
    f"  PAPER TRADE ENTERED: {symbol}\n"
    f"{'═' * 52}\n"
    f"  Setup       : {trade.setup_name}\n"
    f"  Entry price : ${signal.entry_price:.2f}\n"
    f"  Shares      : {sizing.shares}\n"
    f"  Position    : ${sizing.position_value:.2f}\n"
    f"  Stop loss   : ${sizing.initial_stop:.2f}\n"
    f"  T1 (2:1)    : ${targets.t1:.2f}  → sell 50%, stop → breakeven\n"
    f"  After T1    : exit on red candle or extension bar\n"
    f"  Max risk    : ${sizing.risk_amount:.2f}\n"
    f"  Catalyst    : {trade.catalyst_headline[:60]}\n"
    f"{'═' * 52}\n"
)
```

**11. `_limit_sell` → `_exit_sell` (rename + reroute):**
```python
def _exit_sell(client, symbol, qty, log) -> None:
    quote = client.get_quote_with_spread(symbol)
    if quote["wide_spread"]:
        log.warning("%s: wide spread %.1f%% at exit — fast reversal risk", symbol, quote["spread_pct"])
    limit = quote["bid"]  # no haircut
    log.info("%s: exit sell %d @ $%.2f (bid)", symbol, qty, limit)
    client.place_exit_limit_order(symbol, qty, limit)
```
Update **all** call sites in `monitor_position` and the `KeyboardInterrupt` handler.

**12. `monitor_position` — full updated logic:**

Replace the entire take-profit and exit section with this structure:

```python
targets = risk_mgr.calculate_targets(sizing.entry_price, sizing.initial_stop)
shares_remaining = sizing.shares
t1_hit = False
entry_bar_volume = None
last_candle_minute = None

log.info(
    "%s: T1=$%.2f (2:1, sell 50%%) | after T1: stop→breakeven, exit on red-candle or extension-bar",
    symbol, targets.t1,
)

# Capture entry bar volume once
try:
    init_bars = client.get_intraday_bars(symbol, lookback_hours=1)
    entry_bar_volume = float(init_bars["volume"].iloc[-1])
except Exception:
    pass

while True:
    elapsed = time.monotonic() - start
    position = client.get_position(symbol)

    if position is None:
        log.info("%s: position closed externally", symbol)
        _record_exit(trade=trade, exit_price=sizing.entry_price,
                     reason="position_closed_externally", ...)
        break

    current_price = float(position.current_price)

    # ── Per-candle logic (fires once per completed minute bar) ──────────
    current_minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if current_minute != last_candle_minute:
        last_candle_minute = current_minute
        try:
            bars = client.get_intraday_bars(symbol, lookback_hours=1)
            atr = _compute_atr(bars)

            if t1_hit and atr and len(bars) >= 2:
                last_bar = bars.iloc[-2]  # last completed bar

                # Red-candle exit — bearish close after T1
                if risk_mgr.is_red_candle(last_bar):
                    log.info("%s: red candle — exiting remaining %d shares", symbol, shares_remaining)
                    _exit_sell(client, symbol, shares_remaining, log)
                    _record_exit(trade=trade, exit_price=current_price,
                                 reason="red_candle_exit", ...)
                    break

                # Extension-bar exit — climactic / parabolic move
                if risk_mgr.is_extension_bar(last_bar, atr):
                    log.info("%s: extension bar — climactic move, exiting remaining %d shares", symbol, shares_remaining)
                    _exit_sell(client, symbol, shares_remaining, log)
                    _record_exit(trade=trade, exit_price=current_price,
                                 reason="extension_bar_exit", ...)
                    break

                # ATR trailing stop — raises from breakeven each bar
                prev_candle_low = float(bars["low"].iloc[-1])
                candidate = round(prev_candle_low - atr * config.risk.atr_multiplier, 4)
                if candidate > current_stop:
                    log.info("%s: stop raised $%.4f → $%.4f", symbol, current_stop, candidate)
                    current_stop = candidate

            # Stall / momentum-fail exits (before T1 only)
            if not t1_hit and entry_bar_volume:
                bars_since = bars.tail(config.risk.stall_bars)
                if risk_mgr.is_stall(bars_since, sizing.entry_price, targets.t1):
                    log.warning("%s: stall — no progress in %d bars, bailing out", symbol, config.risk.stall_bars)
                    _exit_sell(client, symbol, shares_remaining, log)
                    _record_exit(trade=trade, exit_price=current_price,
                                 reason="stall_exit", ...)
                    break
                if risk_mgr.is_momentum_fail(bars.tail(config.risk.momentum_fail_bars + 1), entry_bar_volume):
                    log.warning("%s: momentum fail — volume collapsed, exiting", symbol)
                    _exit_sell(client, symbol, shares_remaining, log)
                    _record_exit(trade=trade, exit_price=current_price,
                                 reason="momentum_fail", ...)
                    break

        except Exception as exc:
            log.warning("%s: bar fetch failed: %s", symbol, exc)

    # ── T1: 2:1 take-profit — sell 50%, move stop to breakeven ──────────
    if not t1_hit and current_price >= targets.t1:
        t1_shares = shares_remaining // 2
        if t1_shares > 0:
            log.info("%s: T1 hit $%.2f — selling %d shares (50%%)", symbol, targets.t1, t1_shares)
            _exit_sell(client, symbol, t1_shares, log)
            shares_remaining -= t1_shares
        current_stop = sizing.entry_price  # move stop to breakeven immediately
        log.info("%s: stop moved to breakeven $%.2f", symbol, current_stop)
        t1_hit = True

    # ── Hard stop ────────────────────────────────────────────────────────
    if risk_mgr.is_stop_hit(current_price, current_stop):
        reason = "breakeven_stop" if t1_hit else "stop_loss"
        log.warning("%s: stop hit $%.2f — closing %d shares (%s)", symbol, current_stop, shares_remaining, reason)
        _exit_sell(client, symbol, shares_remaining, log)
        _record_exit(trade=trade, exit_price=current_price, reason=reason, ...)
        break

    # ── Max hold time ────────────────────────────────────────────────────
    if elapsed >= config.max_hold_minutes * 60:
        log.info("%s: max hold time reached — closing", symbol)
        client.cancel_orders_for_symbol(symbol)
        _exit_sell(client, symbol, shares_remaining, log)
        _record_exit(trade=trade, exit_price=current_price, reason="max_hold_time", ...)
        break

    time.sleep(interval)
```

**13. `main()` routing:**
```python
if args.scanner:
    from scanner import PremarketScanner, CatalystChecker
    run_scanner_mode(config, args)
else:
    if not args.ticker:
        parser.print_help(); sys.exit(1)
    symbol = validate_ticker(args.ticker)
    run(symbol, config,
        skip_filters=args.skip_filters,
        skip_entry_signal=args.skip_entry_signal,
        args=args)
```

---

## Step 8 — `requirements.txt`

Add if not already present:
```
pytz>=2024.1
```

No other new dependencies — Alpaca news is already in `alpaca-py`.

---

## Verification Checklist

- [ ] `python -c "from config import Config; c=Config(); print(c.filters.min_rvol, c.risk.min_rr_ratio)"` → `5.0 2.0`
- [ ] `python run_bot.py AAPL` → fails `RVOL X.xx < 5.0 minimum`
- [ ] `python run_bot.py TICKER --skip-filters --skip-entry-signal --no-trading-hours-gate --no-catalyst-check` → log shows `T1=... (2:1, sell 50%)` and `stop moved to breakeven`
- [ ] After T1 hit in paper session → next red candle triggers `red_candle_exit`, not EMA logic
- [ ] After T1 hit → `current_stop` equals `entry_price` in logs (breakeven confirmed)
- [ ] `python run_bot.py --scanner --universe universe.txt` → ranked table prints, Alpaca headlines appear, y/n prompt fires
- [ ] Full paper trade cycle → `logs/trades.csv` has `setup_name` and `catalyst_headline`; old rows readable without crash
- [ ] Wide-spread ticker → `spread X% > 2% — skipping`

---

## Constraints

- Paper trading only. `paper_only = True` must never be changed.
- Use `pytz.timezone("America/New_York")` for all ET conversions — handles DST.
- Alpaca `NewsClient` uses existing `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`. No new `.env` keys needed.
- Cache yfinance fundamentals per symbol for 5 minutes in `PremarketScanner`.
- ATR falls back to `initial_stop_offset` when fewer than 14 bars are available (`min_periods=1`).
- Old `logs/trades.csv` rows must not crash — use `restval=""` in `DictReader`.
- Never use T2 fixed price or 9-EMA exit. Those are removed.
- Watch for: fast reversals, widened spreads, hidden sellers, halts.
