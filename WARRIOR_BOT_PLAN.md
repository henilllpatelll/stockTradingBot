# Plan: Warrior Trading — Full Autonomous AI Bot

## Context
The codebase already has solid infrastructure (Alpaca trading + data, 5-pillar scanner, EDGAR integration, FastAPI + React frontend). What's missing is:
1. The 8 entry setup patterns (the "when to pull the trigger" logic)
2. Proper exit logic (red-candle, extension-bar, T3 completion)
3. A clean, purpose-built AI brain for live trading decisions (replace the existing catalyst_agent.py approach)
4. A fully wired autonomous execution pipeline (news → AI → pattern → execute → manage)

**Key user decisions:**
- **AI model:** `claude-sonnet-4-6` with aggressive prompt caching (fast + smart — the AI is the primary decision-maker)
- **Trading mode:** Paper trading only (Alpaca sandbox, `paper=True` stays)
- **Entry gate:** At least 1 confirmed setup pattern REQUIRED before any trade fires

The existing `catalyst_agent.py` is the batch analysis tool for historical review — it stays as-is. The live trading AI is a clean, standalone `news_evaluator.py` using Sonnet.

---

## Implementation Stages (Priority Order)

### Stage 1 — Foundation (unblocks everything)

#### `config.py`
- Fix `Config.trading_start_hour_et = 7`, `trading_end_hour_et = 11` (was 4/20 — scanner still runs 4-20)
- Add to `RiskConfig`: `min_rr_ratio=2.0`, `extension_bar_atr_mult=2.0`, `stall_bars=3`, `stall_progress_threshold=0.10`, `momentum_fail_vol_ratio=0.50`, `momentum_fail_bars=2`, `entry_fill_timeout_seconds=15`
- Add new `NewsEvaluatorConfig` dataclass with: `enabled=True`, `model="claude-haiku-4-5-20251001"`, `confidence_threshold=0.55`, `setup_confidence_overrides` dict (per-setup thresholds), `market_regime="hot"`, `fallback_to_manual=False`, `timeout_seconds=15.0`
- Add `news_evaluator: NewsEvaluatorConfig` field to root `Config`

#### `alpaca_client.py`
- Add `get_quote_with_spread(symbol) -> dict` — returns ask/bid/spread_pct/wide_spread (>2%)
- Add `place_entry_limit_order(symbol, qty, ask)` — limit at ask+$0.02
- Add `place_exit_limit_order(symbol, qty, bid)` — limit at bid (no haircut)
- Add `get_premarket_bars(symbol, date=None) -> pd.DataFrame` — 1-min bars from 4:00–9:29 AM ET (critical for ORB patterns)
- Upgrade `get_news()`: swap raw HTTP for `alpaca.data.historical.NewsClient` (same credentials)

#### `risk_manager.py`
- Simplify `TakeProfitTargets` to just `t1: float` (2:1 R/R only; T2/T2_psych removed per BUILD_SPEC)
- Update `calculate_targets()`: `t1 = entry + 2 × risk`
- Update `calculate_position_size()`: stop priority = `setup_stop_ref` > ATR-based > initial_stop_offset fallback
- Add `is_red_candle(bar: pd.Series) -> bool` — `close < open`
- Add `is_extension_bar(bar: pd.Series, atr: float) -> bool` — range > `atr × extension_bar_atr_mult`
- Add `is_stall(bars_since_entry, entry_price, t1_price) -> bool` — price made < 10% progress toward T1 after N bars
- Add `is_momentum_fail(bars, entry_bar_volume) -> bool` — volume < 50% of entry bar for 2 consecutive bars

#### `trade_logger.py`
- Add `setup_name: str = ""` and `catalyst_headline: str = ""` to `TradeRecord` and `_FIELDS`
- Change `DictReader` to `DictReader(fh, restval="")` to avoid crashes on old CSVs

---

### Stage 2 — Core Trading Logic

#### `strategy.py` (largest addition)
Add `PremkLevels` dataclass:
```python
@dataclass
class PremkLevels:
    premarket_high: float
    premarket_low: float
    vwap: float
    pivots: list[dict]   # {"type": "high"|"low", "price": float, "bar_index": int}
    flags: list[dict]    # {"top": float, "bottom": float, "start_index": int, "end_index": int}
```

Add `mark_premarket_levels(bars: pd.DataFrame) -> PremkLevels`:
- pm_high = bars["high"].max(), pm_low = bars["low"].min()
- vwap = sum(typical_price × volume) / sum(volume) where typical_price = (H+L+C)/3
- pivots = bars where high[i] > high[i-1] and high[i] > high[i+1] (vice versa for lows)
- flags = 4-bar windows with range < 0.5% of low (tight consolidation zones)

Add 8 setup detectors — each returns `(triggered: bool, name: str, trigger_price: float, stop_ref: float|None)`:

| Function | Trigger Condition | stop_ref |
|---|---|---|
| `detect_premarket_high_break(bars, pm_high)` | last close > pm_high | None (ATR-based) |
| `detect_pm_flag_break(bars, flags)` | last close > latest flag top | flag bottom |
| `detect_pm_pivot_break(bars, pivots)` | last close > last pivot high | last pivot low |
| `detect_red_to_green(bars, prev_close)` | prev bar < prev_close AND last bar >= prev_close | prev_close × 0.995 |
| `detect_1min_orb(bars, session_open_time)` | last close > 9:30 candle high | 9:30 candle low |
| `detect_5min_orb(bars, session_open_time)` | last close > first-5-min high | first-5-min low |
| `detect_first_pullback_bull_flag(bars)` | impulse + consolidation + break above | consolidation bottom |
| `detect_flat_top_breakout(bars)` | 2+ equal highs within price×0.3% tolerance, then break | None |

Add `run_setup_detectors(bars, pm_levels, prev_close, session_open_time=None) -> list` — calls all 8, returns all results.

Update `EntrySignal` dataclass: add `setup_name: str = ""`, `catalyst_headline: str = ""`, `setup_stop_ref: float | None = None`.

Update `check_entry_signal()`: call `run_setup_detectors()` after volume acceleration gate passes, populate `signal.setup_name` and `signal.setup_stop_ref` from first triggered setup.

#### `run_bot.py`
- Add module-level `_compute_atr(bars) -> float | None` (true range rolling 14 periods)
- Add `--scanner`, `--universe PATH`, `--no-catalyst-check`, `--no-trading-hours-gate` CLI args
- **Trading hours gate** at top of `run()`: block execution outside 7–11 AM ET unless `--no-trading-hours-gate`
- **Wide-spread gate** after filters pass: skip if spread > 2%
- **Catalyst AI block**: call `NewsCatalystAgent.evaluate()`, return if `should_trade=False`
- **Premarket level snapshot**: call `get_premarket_bars()` → `mark_premarket_levels()` before entry
- **Setup detection block**: call `run_setup_detectors()` on live bars, log active setups
- Rewrite `monitor_position()` exit logic:
  - T1 = 50% sell at 2× risk (not 1×), then stop moves to breakeven
  - After T1: red-candle exit (`is_red_candle(bars.iloc[-2])`) OR extension-bar exit (`is_extension_bar(bars.iloc[-2], atr)`)
  - Before T1: stall exit (`is_stall(...)`) OR momentum-fail exit (`is_momentum_fail(...)`)
  - Hard stop and max hold (120 min) unchanged
- Replace `_limit_sell` with `_exit_sell()` using `place_exit_limit_order()` (bid, no haircut)
- Update entry order: `place_entry_limit_order()` at ask+$0.02, 15s timeout

---

### Stage 3 — AI Enhancement

#### `news_evaluator.py` (new file — the AI brain for live trading)
Clean standalone AI module using **claude-sonnet-4-6** with prompt caching for speed:

```python
@dataclass
class TradeDecision:
    should_trade: bool
    confidence: float          # 0.0–1.0
    catalyst_quality: str      # "strong" | "medium" | "weak" | "negative"
    catalyst_type: str         # "earnings" | "PR" | "FDA" | "offering" | "halt_resume" | etc.
    setup_quality: str         # "A+" | "A" | "B" | "skip"
    reasoning: str             # short explanation (2-3 sentences)
    risk_flags: list[str]      # ["dilution_risk", "extended_premarket", "hidden_seller", ...]
    entry_price_target: float  # AI suggested entry (ask + slippage guidance)
    stop_suggestion: float     # AI suggested stop

class TradingAI:
    def __init__(model="claude-sonnet-4-6", ...)
    def assess(symbol, headlines, filter_data, setup_name, pattern_ctx) -> TradeDecision
```
- Model: `claude-sonnet-4-6` (smarter reasoning on borderline setups)
- System prompt cached with `cache_control: {"type": "ephemeral"}` — billed once per 5-min window
- Tool use with forced `trade_assessment` tool — structured JSON response, no freeform parsing
- `should_trade = confidence >= effective_threshold AND setup_quality in ["A+", "A"]`
- Entry requires: `should_trade=True` AND at least 1 confirmed setup pattern from `run_setup_detectors()`
- Error handling: API error → `should_trade=False`, `risk_flags=["api_error"]`

The system prompt encodes all Warrior Trading rules: 5 pillars, catalyst quality rubric, pattern-specific confidence modifiers, dilution/halt risk signals, "breakout or bailout" urgency.

Add `PatternContext` dataclass (in `strategy.py`, imported by `news_evaluator.py`):
```python
@dataclass
class PatternContext:
    setup_name: str
    pm_high: float; pm_low: float; vwap: float
    current_price: float
    price_vs_vwap_pct: float     # (price - vwap) / vwap × 100
    price_vs_pm_high_pct: float  # (price - pm_high) / pm_high × 100
    active_setups: list[str]
```

#### `catalyst_agent.py` (no changes)
Left as-is — it's the batch historical analyzer for the Analyzer tab in the frontend. The live trading AI brain is entirely in `news_evaluator.py`.

---

### Stage 4 — Autonomous Pipeline

#### `news_stream.py`
Replace `_execute_trade()` stub with full async pipeline:
```
Alpaca WebSocket news event
  → _handle_event() [scanner filters]
    → _execute_trade() [async task]
        ├─ is_trading_hours() gate (7–11 AM ET)
        ├─ get_quote_with_spread() gate (< 2%)
        ├─ get_premarket_bars() → mark_premarket_levels()
        ├─ get_intraday_bars() → run_setup_detectors()
        ├─ NewsCatalystAgent.evaluate() [AI: catalyst + pattern + regime]
        └─ run() [if approved: size → entry order → monitor_position()]
```

Add `_AutoArgs` namedtuple (duck-types argparse.Namespace for `run()`).

Add `_active_trades: set[str]` guard in `NewsStreamBot.__init__()` — prevents duplicate concurrent trades on same symbol.

Add daily reset for `self._alerted` set at midnight ET.

---

### Stage 5 — Frontend + API

#### `api.py`
- Add `POST /api/trade` endpoint → validates symbol → dispatches `asyncio.create_task(_run_trade_task(...))`
- Add `GET /api/positions` endpoint → returns open Alpaca positions
- Update `_scanner_task()`: add `get_premarket_bars()` call per candidate (with module-level `_pm_levels_cache` dict to avoid duplicate calls before 9:30 AM), run `run_setup_detectors()`, add `active_setups` + `pm_high` + `pm_low` to candidate output dict

#### `frontend/src/pages/Scanner.tsx`
- Add `active_setups: string[]`, `pm_high?: number`, `pm_low?: number` to `ScannerCandidate` interface
- Add setup badges per candidate card (compact tag pills)
- Add trade button per candidate with confirmation modal (shows: symbol, price, gap%, RVOL, setups → "Execute paper trade?" Confirm/Cancel)

#### `frontend/src/pages/Newsfeed.tsx`
- Add `active_setups?: string[]` to `NewsAlert` interface
- Add trade button per alert (same pattern as Scanner.tsx)

#### `frontend/src/types.ts`
- Add `TradeExecution` interface: `{ status: 'dispatched' | 'error', symbol: string, message?: string }`
- Add `Position` interface: `{ symbol, qty, entry_price, current_price, unrealized_pl }`

---

## Architectural Risks to Guard Against

| Risk | Guard |
|---|---|
| ORB bar timestamp alignment | Match bars where ET hour==9 AND minute==30, not exact UTC equality |
| `mark_premarket_levels()` on empty bars | Return `None`, short-circuit all downstream pattern calls on `None` |
| Concurrent duplicate trades | `_active_trades: set[str]` in NewsStreamBot, try/finally discard |
| `targets.t2` attribute error | Grep and remove all `targets.t2` / `targets.t2_psych` references before changing dataclass |
| Scanner task API explosion | `_pm_levels_cache: dict[str, PremkLevels]` in api.py, populated once per symbol until 9:30 AM |
| Red-candle on in-progress bar | Always use `bars.iloc[-2]` (last completed candle) for exit checks |
| `flat_top` tolerance for cheap stocks | Use price-relative tolerance: `max($0.03, price × 0.003)` |
| CSV concurrency (2 trades exiting at once) | Document limitation; leave for future file-lock fix |
| Claude API latency > 15s | Log latency, rely on `timeout_seconds=15.0` config, return `should_trade=False` on timeout |
| DST boundary in `get_premarket_bars()` | Always use `pytz.timezone("America/New_York").localize(datetime(...))`, never raw timedelta |

---

## Critical Files to Modify

| File | Stage | Status |
|---|---|---|
| `config.py` | 1 | Existing — edit only |
| `alpaca_client.py` | 1 | Existing — add 4 methods |
| `risk_manager.py` | 1 | Existing — add 4 methods + simplify targets |
| `trade_logger.py` | 1 | Existing — add 2 fields |
| `strategy.py` | 2 | Existing — add ~150 lines (patterns, PremkLevels) |
| `run_bot.py` | 2 | Existing — add gates, rewrite monitor loop |
| `news_evaluator.py` | 3 | **New file** (~180 lines) — Sonnet-powered AI brain |
| `catalyst_agent.py` | — | Untouched — historical batch analysis only |
| `news_stream.py` | 4 | Existing — rewrite `_execute_trade()` |
| `api.py` | 5 | Existing — add 2 endpoints + scanner enhancement |
| `frontend/src/pages/Scanner.tsx` | 5 | Existing — add trade button + badges |
| `frontend/src/pages/Newsfeed.tsx` | 5 | Existing — add trade button |
| `frontend/src/types.ts` | 5 | Existing — add 2 interfaces |

---

## Verification / Testing

1. **Unit test setup detectors**: Call each with synthetic 1-min bar DataFrames, confirm `triggered=True` at the right price level.
2. **Premarket levels smoke test**: `python -c "from alpaca_client import AlpacaClient; from config import Config; c = AlpacaClient(Config()); print(c.get_premarket_bars('AAPL').tail())"` — confirm bars stop at 9:29 AM ET.
3. **Wide-spread gate**: Call `get_quote_with_spread()` on a low-float stock premarket, confirm `wide_spread=True` blocks entry.
4. **AI evaluation test**: Run `NewsCatalystAgent.evaluate("TSLA", ["Tesla beats earnings"], rvol=8.0, gap_pct=12.0, price=15.0)` — confirm structured `CatalystEvaluation` returned in < 15s.
5. **Full autonomous dry run**: Start `python news_stream.py --auto-trade`, wait for a news event from Alpaca, confirm pipeline logs: trading hours gate → spread gate → PM levels → setup detection → AI eval → entry/skip decision.
6. **Monitor loop exit test**: Start `python run_bot.py SYMBOL --skip-filters --skip-entry-signal`, simulate position via Alpaca paper, confirm red-candle exit fires on first bearish close after T1.
7. **Frontend trade button**: Start FastAPI (`uvicorn api:app`), open browser, click Trade on a scanner candidate, confirm `POST /api/trade` returns `{"status": "dispatched"}` and position appears in Alpaca paper account.
