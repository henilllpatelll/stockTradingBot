# Premarket Catalyst Paper-Trading Bot

A Python paper-trading bot for Alpaca Markets that screens for premarket catalyst setups and simulates trades on a paper account. **No live trading — paper account only.**

---

## Strategy: Premarket Catalyst

Targets low-float, small-cap tickers gapping up strongly on premarket volume. The bot will only enter if all five filters pass **and** a momentum entry signal is confirmed.

### Filters
| Filter | Default | Description |
|--------|---------|-------------|
| Price range | $2 – $20 | Small-cap sweet spot |
| Float | < 20 M shares | Thin float amplifies moves |
| Market cap | < $2 B | Micro/small-cap only |
| Change vs. prev. close | > 5 % | Gap catalyst required |
| RVOL | > 1.0× | Volume above daily average rate |

### Entry confirmation
After filters pass, the bot checks the most recent minute bars for:
- **Volume acceleration** ≥ 1.2× (last 5 bars vs. prior bars)
- **Price strength**: recent lows trending upward

### Exit
- **Trailing stop** (default 5 %, Alpaca native order) follows the price up automatically
- **Hard stop loss** (default 7 %) as a fallback
- **Max hold time** (default 120 min) force-closes any open position

---

## Setup

### 1. Prerequisites
- Python 3.10+
- A free [Alpaca Markets](https://alpaca.markets) account

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file

```bash
cp .env.example .env
```

Then edit `.env` and paste your **Paper Trading** API keys:

```
ALPACA_API_KEY=PKTEST_xxxxxxxxxxxxxxxxxxxx
ALPACA_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

#### How to get Alpaca paper API keys
1. Sign in at [https://alpaca.markets](https://alpaca.markets)
2. In the top-left corner, switch the environment toggle to **Paper**
3. Go to **Overview → API Keys → Generate New Key**
4. Copy the API Key ID and Secret Key into your `.env`

> **Important:** Paper keys start with `PKTEST_`. If yours starts with `PK` without `TEST`, you are looking at live keys — switch to the Paper environment first.

---

## Configuration

All settings live in `config.py`. You can also override the most common ones via CLI flags (see below). No code changes needed for typical usage.

```python
# config.py — key knobs

FilterConfig:
    min_price          = 2.0        # Lower bound on price filter ($)
    max_price          = 20.0       # Upper bound on price filter ($)
    max_float_shares   = 20_000_000 # Maximum float
    max_market_cap     = 2_000_000_000
    min_change_pct     = 5.0        # Minimum % gap from previous close
    min_rvol           = 1.0        # Minimum relative volume

RiskConfig:
    max_position_pct   = 0.02       # 2% of account equity per trade
    stop_loss_pct      = 0.07       # 7% hard stop loss
    trailing_stop_pct  = 0.05       # 5% trailing stop
    max_daily_loss_pct = 0.06       # 6% daily loss ceiling

Config:
    monitor_interval_seconds = 30   # Poll interval while in a position
    max_hold_minutes         = 120  # Force-close after 2 hours
```

---

## How to Run

### Analyse a ticker and trade if conditions are met

```bash
python run_bot.py BBAI
```

### Override risk settings from the command line

```bash
python run_bot.py MSTR --stop-loss 8 --trail 6 --max-position 1.5
```

| Flag | Description | Example |
|------|-------------|---------|
| `--stop-loss PCT` | Hard stop loss % (5–10 recommended) | `--stop-loss 7` |
| `--trail PCT` | Trailing stop % | `--trail 5` |
| `--max-position PCT` | Position size as % of account equity | `--max-position 2` |
| `--skip-entry-signal` | Skip momentum confirmation (testing only) | |
| `--summary` | Print trade history and exit | |

### Print trade history

```bash
python run_bot.py --summary
```

---

## Output

Logs are written to `logs/`:

| File | Contents |
|------|----------|
| `logs/bot.log` | Full timestamped run log |
| `logs/trades.csv` | One row per trade — entry, exit, PnL, reason |

Example terminal output after a trade:

```
====================================================
  PAPER TRADE ENTERED: BBAI
====================================================
  Entry price : $4.37
  Shares      : 45
  Position    : $196.65
  Stop loss   : $4.06  (7%)
  Trail stop  : 5%
  Max risk    : $13.95
  Signal      : vol_accel=1.84x, change=+12.3%, RVOL=3.41
====================================================

[monitoring every 30 s ...]

────────────────────────────────────────────
  Paper Trade Summary
────────────────────────────────────────────
  Total trades  : 3
  Open          : 0
  Closed        : 3
  Win rate      : 67%  (2W / 1L)
  Total PnL     : $+41.20
  Average PnL   : $+13.73
  Average win   : $+32.10
  Average loss  : $-22.80
────────────────────────────────────────────
```

---

## Project Structure

```
stock-trading/
├── .env.example        # API key template
├── requirements.txt    # Python dependencies
├── README.md
├── config.py           # All settings (Alpaca keys, filters, risk)
├── alpaca_client.py    # Alpaca paper API wrapper (quotes, bars, orders)
├── strategy.py         # Premarket catalyst filter + entry signal logic
├── risk_manager.py     # Position sizing, stop management, daily loss check
├── trade_logger.py     # CSV trade log + summary printer
├── run_bot.py          # CLI entry point
└── logs/               # Created automatically at runtime
    ├── bot.log
    └── trades.csv
```

---

## Notes & Limitations

- **Paper trading only.** The `paper_only = True` guard in `config.py` is hard-coded and cannot be disabled.
- **Data source:** Quotes and bars come from Alpaca's free IEX feed. Float and market-cap data come from `yfinance` and may be delayed or unavailable for very thinly-traded tickers.
- **RVOL during premarket:** Calculated against a normalized daily-volume baseline. The first few bars of the session will show inflated RVOL — treat values < 2× cautiously before 5 AM ET.
- **Alpaca data subscription:** Extended-hours (premarket) minute bars require at least the free Unlimited plan on Alpaca. If bars are empty, verify your data subscription at [alpaca.markets](https://alpaca.markets).
