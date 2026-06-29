"""Fetch 7-9:30 AM ET news and apply all scanner filters: price, float, mktcap, gap, RVOL."""
import requests
import time
from datetime import datetime, timezone, timedelta
import pytz
import yfinance as yf

KEY = "PK642KC6EURGTCROMAX6AUWKJA"
SECRET = "De8e3B9rf5aFmiWWLVXRBju7eYW1bgxHRQiNEVb4uQAo"
HEADERS = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET}

# Scanner filter config (from config.py)
MIN_PRICE       = 2.0
MAX_PRICE       = 20.0
MAX_FLOAT       = 10_000_000
MAX_MARKET_CAP  = 2_000_000_000
MIN_GAP_PCT     = 10.0
MIN_RVOL        = 5.0

SKIP = {
    "SPY","QQQ","IWM","DIA","GLD","SLV","XLF","XLK","XLE","XLV","XLI",
    "XLB","XLC","XLU","XLY","XLP","XLRE","SOXX","FTXL","VGK","BTCUSD","XRPUSD"
}

ET = pytz.timezone("America/New_York")

# --- fetch news ---
all_news = []
next_token = None
while True:
    params = "start=2026-05-21T11:00:00Z&end=2026-05-21T13:30:00Z&limit=50&sort=asc"
    if next_token:
        params += f"&page_token={next_token}"
    resp = requests.get(f"https://data.alpaca.markets/v1beta1/news?{params}", headers=HEADERS, timeout=10)
    data = resp.json()
    all_news += data.get("news", [])
    next_token = data.get("next_page_token")
    if not next_token or len(all_news) >= 300:
        break

# --- build symbol -> articles map ---
news_by_sym: dict = {}
for item in all_news:
    syms = item.get("symbols") or []
    dt = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")).astimezone(ET)
    et_time = dt.strftime("%I:%M %p").lstrip("0")
    for sym in syms:
        if not sym or sym in SKIP:
            continue
        if not sym.replace(".", "").isalpha():
            continue
        if ":" in sym:
            continue
        if sym not in news_by_sym:
            news_by_sym[sym] = []
        news_by_sym[sym].append((et_time, item.get("headline", "")))

print(f"Unique symbols to check: {len(news_by_sym)}")

def get_avg_volume(sym):
    """30-day average daily volume from Alpaca bars."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=44)
    r = requests.get(
        f"https://data.alpaca.markets/v2/stocks/{sym}/bars",
        headers=HEADERS,
        params={"timeframe": "1Day", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 30, "feed": "sip"},
        timeout=8,
    )
    if r.status_code != 200:
        return 0.0
    bars = r.json().get("bars") or []
    if not bars:
        return 0.0
    return sum(b["v"] for b in bars) / len(bars)

# --- apply all filters ---
passing = []
for sym in sorted(news_by_sym.keys()):
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{sym}/snapshot",
            headers=HEADERS,
            params={"feed": "sip"},
            timeout=5,
        )
        if r.status_code != 200:
            continue
        snap = r.json()

        lt = snap.get("latestTrade") or {}
        lq = snap.get("latestQuote") or {}
        price = float(lt.get("p") or lq.get("ap") or 0)

        # Price filter
        if not price or price < MIN_PRICE or price > MAX_PRICE:
            continue

        pdb = snap.get("prevDailyBar") or {}
        prev_close = float(pdb.get("c") or 0)
        if not prev_close:
            continue

        # Gap filter
        gap_pct = (price - prev_close) / prev_close * 100
        if gap_pct < MIN_GAP_PCT:
            continue

        # RVOL filter
        db = snap.get("dailyBar") or {}
        today_vol = float(db.get("v") or 0)
        avg_vol = get_avg_volume(sym)
        rvol = (today_vol / avg_vol) if avg_vol > 0 else 0.0
        if today_vol > 0 and rvol < MIN_RVOL:
            continue

        # Float and market cap (yfinance)
        info = yf.Ticker(sym).info
        float_sh = float(info.get("floatShares") or 0)
        mkt_cap = float(info.get("marketCap") or 0)
        if float_sh <= 0 or float_sh >= MAX_FLOAT:
            continue
        if mkt_cap > 0 and mkt_cap > MAX_MARKET_CAP:
            continue

        passing.append((sym, price, prev_close, gap_pct, rvol, float_sh, mkt_cap, news_by_sym[sym]))
        time.sleep(0.05)
    except Exception:
        continue

print(f"Passing ALL filters: {len(passing)}\n")

for sym, price, prev_close, gap_pct, rvol, flt, mc, articles in sorted(passing, key=lambda x: x[7][0][0]):
    mc_str = f"${mc/1e6:.0f}M" if mc > 0 else "N/A"
    rvol_str = f"{rvol:.1f}x" if rvol > 0 else "N/A"
    print(f"=== {sym}  ${price:.2f}  Gap +{gap_pct:.1f}%  RVOL {rvol_str}  Float {flt/1e6:.1f}M  MCap {mc_str} ===")
    for t, hl in articles:
        print(f"  {t} | {hl}")
    print()
