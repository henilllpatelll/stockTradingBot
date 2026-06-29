"""
news_stream.py — Real-time Alpaca WebSocket news stream.

Fires an alert the moment Alpaca receives a headline for a symbol that passes
the premarket gapper filters (price, float, market cap, gap %, RVOL). No polling.

Usage:
    python news_stream.py              # alert-only mode
    python news_stream.py --auto-trade # alert + execute paper trade
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytz
import requests

from alpaca_client import AlpacaClient
from config import Config
from scanner import PremarketScanner

logger = logging.getLogger("news_stream")
_ET = pytz.timezone("America/New_York")

_WS_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
_RECONNECT_DELAY_S = 5
_MAX_RECONNECT_DELAY_S = 60
_PENDING_RECHECK_S = 15    # how often to recheck gap%/RVOL for pending symbols
_PENDING_MAX_AGE_S = 3600  # drop a pending symbol after 60 minutes with no momentum


def _et_now() -> str:
    return datetime.now(_ET).strftime("%H:%M:%S ET")


def setup_logging(log_dir: str) -> None:
    Path(log_dir).mkdir(exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(Path(log_dir) / "news_stream.log"),
        ],
    )


def _impact(rvol: float) -> str:
    if rvol >= 10:
        return "High"
    if rvol >= 5:
        return "Medium"
    return "Low"


class NewsStreamBot:
    def __init__(
        self,
        config: Config,
        on_alert=None,
        auto_trade: bool = False,
    ) -> None:
        self._cfg = config
        self._on_alert = on_alert  # async callback(alert: dict) — set by api.py
        self._auto_trade = auto_trade
        self._client = AlpacaClient(config)
        self._scanner = PremarketScanner(config, self._client)
        self._alerted: set[str] = set()
        self._pending: dict[str, dict] = {}  # symbol -> {headline, source, added_at}

    # ------------------------------------------------------------------ #
    # WebSocket connection                                                 #
    # ------------------------------------------------------------------ #

    async def _connect(self):
        try:
            import websockets
        except ImportError:
            logger.error(
                "websockets package not found. Run: pip install websockets>=11.0"
            )
            sys.exit(1)
        return websockets.connect(_WS_URL)

    async def _auth_and_subscribe(self, ws) -> bool:
        # Expect initial connected message
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        logger.debug("connected msg: %s", msg)

        await ws.send(json.dumps({
            "action": "auth",
            "key": self._cfg.alpaca.api_key,
            "secret": self._cfg.alpaca.secret_key,
        }))
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        resp = json.loads(raw)
        logger.debug("auth resp: %s", resp)

        # Check auth succeeded — message looks like [{"T":"success","msg":"authenticated"}]
        events = resp if isinstance(resp, list) else [resp]
        if not any(e.get("msg") == "authenticated" for e in events):
            logger.error("Authentication failed: %s", resp)
            return False

        await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        logger.info("Subscribed: %s", json.loads(raw))
        return True

    # ------------------------------------------------------------------ #
    # News handler                                                        #
    # ------------------------------------------------------------------ #

    async def _handle_event(self, event: dict) -> None:
        if event.get("T") != "n":
            return

        created_at = event.get("created_at", "")
        if created_at:
            try:
                _ET = pytz.timezone("America/New_York")
                event_date = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(_ET).date()
                if event_date < datetime.now(_ET).date():
                    logger.debug("Skipping stale news from %s", event_date)
                    return
            except Exception:
                pass

        headline = event.get("headline", "")
        source = event.get("source", "")
        symbols: list[str] = event.get("symbols") or []

        for symbol in symbols:
            if not symbol or symbol in self._alerted or symbol in self._pending:
                continue
            if not symbol.replace(".", "").isalpha():
                continue

            # Filter check in thread pool — avoids blocking the WS event loop
            candidate, only_momentum_failed = await asyncio.to_thread(
                self._scanner._check_symbol_with_detail, symbol
            )
            if candidate is None:
                if only_momentum_failed:
                    self._pending[symbol] = {
                        "headline": headline,
                        "source": source,
                        "added_at": time.time(),
                    }
                    logger.info(
                        "PENDING %s — static filters passed, watching gap%%/RVOL | %s",
                        symbol, headline,
                    )
                else:
                    logger.debug("%s: news arrived but failed static filters — skip", symbol)
                continue

            self._alerted.add(symbol)

            alert = {
                "id": f"{symbol}_{int(time.time())}",
                "time": _et_now(),
                "symbol": symbol,
                "headline": headline,
                "source": source,
                "price": candidate.price,
                "gap_pct": candidate.gap_pct,
                "rvol": candidate.rvol,
                "float_shares": candidate.float_shares,
                "impact": _impact(candidate.rvol),
            }

            logger.info(
                "ALERT %s +%.1f%% RVOL %.1fx | %s", symbol, candidate.gap_pct, candidate.rvol, headline
            )

            if self._on_alert is not None:
                await self._on_alert(alert)
            else:
                print(
                    f"\n{'═' * 58}\n"
                    f"  *** LIVE NEWS ALERT  [{_et_now()}] ***\n"
                    f"{'═' * 58}\n"
                    f"  Symbol  : {symbol}\n"
                    f"  Headline: {headline}\n"
                    f"  Source  : {source}\n"
                    f"  Price   : ${candidate.price:.2f}\n"
                    f"  Gap     : +{candidate.gap_pct:.1f}%\n"
                    f"  RVOL    : {candidate.rvol:.1f}x\n"
                    f"  Float   : {candidate.float_shares / 1e6:.1f}M\n"
                    f"{'═' * 58}\n"
                )

            if self._auto_trade:
                asyncio.create_task(self._execute_trade(symbol, headline, candidate))

    async def _recheck_pending_loop(self) -> None:
        """Background task: recheck gap%/RVOL for pending symbols every 15 seconds."""
        while True:
            await asyncio.sleep(_PENDING_RECHECK_S)
            if not self._pending:
                continue

            now = time.time()
            expired = [s for s, d in self._pending.items() if now - d["added_at"] > _PENDING_MAX_AGE_S]
            for sym in expired:
                logger.info("%s: pending watchlist expired (60min) — removing", sym)
                del self._pending[sym]

            for symbol, data in list(self._pending.items()):
                candidate = await asyncio.to_thread(self._scanner.recheck_momentum, symbol)
                if candidate is None:
                    continue

                del self._pending[symbol]
                self._alerted.add(symbol)
                headline = data["headline"]
                source = data["source"]

                alert = {
                    "id": f"{symbol}_{int(time.time())}",
                    "time": _et_now(),
                    "symbol": symbol,
                    "headline": headline,
                    "source": source,
                    "price": candidate.price,
                    "gap_pct": candidate.gap_pct,
                    "rvol": candidate.rvol,
                    "float_shares": candidate.float_shares,
                    "impact": _impact(candidate.rvol),
                }

                logger.info(
                    "MOMENTUM ALERT %s +%.1f%% RVOL %.1fx (was pending) | %s",
                    symbol, candidate.gap_pct, candidate.rvol, headline,
                )

                if self._on_alert is not None:
                    await self._on_alert(alert)
                else:
                    print(
                        f"\n{'═' * 58}\n"
                        f"  *** MOMENTUM ALERT  [{_et_now()}] ***\n"
                        f"{'═' * 58}\n"
                        f"  Symbol  : {symbol}\n"
                        f"  Headline: {headline}\n"
                        f"  Source  : {source}\n"
                        f"  Price   : ${candidate.price:.2f}\n"
                        f"  Gap     : +{candidate.gap_pct:.1f}%\n"
                        f"  RVOL    : {candidate.rvol:.1f}x\n"
                        f"  Float   : {candidate.float_shares / 1e6:.1f}M\n"
                        f"{'═' * 58}\n"
                    )

                if self._auto_trade:
                    asyncio.create_task(self._execute_trade(symbol, headline, candidate))

    async def _execute_trade(self, symbol: str, headline: str, candidate) -> None:
        from run_bot import run
        logger.info("%s: auto-trade dispatched | catalyst: %s", symbol, headline)
        try:
            await asyncio.to_thread(
                run, symbol, self._cfg, True, True  # skip_filters=True, skip_entry_signal=True
            )
        except Exception as exc:
            logger.error("%s: auto-trade failed: %s", symbol, exc)

    # ------------------------------------------------------------------ #
    # Main loop with reconnect                                            #
    # ------------------------------------------------------------------ #

    async def _run_once(self) -> None:
        async with await self._connect() as ws:
            if not await self._auth_and_subscribe(ws):
                return
            print(f"[{_et_now()}] News stream live. Filters active. Waiting for headlines...\n")
            async for raw in ws:
                events = json.loads(raw)
                if not isinstance(events, list):
                    events = [events]
                for event in events:
                    asyncio.create_task(self._handle_event(event))

    async def _run_with_reconnect(self) -> None:
        asyncio.create_task(self._recheck_pending_loop())
        delay = _RECONNECT_DELAY_S
        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Stream disconnected (%s) — reconnecting in %ds", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, _MAX_RECONNECT_DELAY_S)
            else:
                # _run_once returned cleanly — auth failure or clean disconnect
                await asyncio.sleep(delay)
                delay = min(delay * 2, _MAX_RECONNECT_DELAY_S)

    def run(self) -> None:
        asyncio.run(self._run_with_reconnect())


# ------------------------------------------------------------------ #
# GlobeNewswire RSS poller                                           #
# ------------------------------------------------------------------ #

class GlobeNewswirePoller:
    """Polls GlobeNewswire RSS and fires alerts for tickers that pass filters."""

    _SYMBOL_RE = re.compile(
        r'\((?:NASDAQ|NYSE(?:\s+American)?|AMEX|OTC(?:[A-Z]*)?):\s*([A-Z]{1,5})\)',
        re.IGNORECASE,
    )

    def __init__(self, config: Config, on_alert=None) -> None:
        self._cfg = config
        self._gnw_cfg = config.globenewswire
        self._on_alert = on_alert
        self._client = AlpacaClient(config)
        self._scanner = PremarketScanner(config, self._client)
        self._seen_guids: set[str] = set()
        self._alerted: set[str] = set()
        self._pending: dict[str, dict] = {}  # symbol -> {headline, source, added_at}

    def _fetch_items(self) -> list[dict]:
        resp = requests.get(
            self._gnw_cfg.feed_url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = []
        for item in root.findall(".//item"):
            guid = (item.findtext("guid") or item.findtext("link") or "").strip()
            title = (item.findtext("title") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            items.append({"guid": guid, "title": title, "pub_date": pub_date})
        return items

    def _is_today(self, pub_date: str) -> bool:
        if not pub_date:
            return True  # no date — assume current
        try:
            dt = parsedate_to_datetime(pub_date).astimezone(_ET)
            return dt.date() >= datetime.now(_ET).date()
        except Exception:
            return True

    def _extract_symbols(self, text: str) -> list[str]:
        return list({m.upper() for m in self._SYMBOL_RE.findall(text)})

    async def _poll_once(self) -> None:
        # Recheck pending symbols — expire old ones, fire alert when momentum arrives
        now = time.time()
        expired = [s for s, d in self._pending.items() if now - d["added_at"] > _PENDING_MAX_AGE_S]
        for sym in expired:
            logger.info("GNW %s: pending watchlist expired (60min) — removing", sym)
            del self._pending[sym]

        for symbol, pdata in list(self._pending.items()):
            candidate = await asyncio.to_thread(self._scanner.recheck_momentum, symbol)
            if candidate is None:
                continue
            del self._pending[symbol]
            self._alerted.add(symbol)
            alert = {
                "id": f"gnw_{symbol}_{int(time.time())}",
                "time": _et_now(),
                "symbol": symbol,
                "headline": pdata["headline"],
                "source": pdata["source"],
                "price": candidate.price,
                "gap_pct": candidate.gap_pct,
                "rvol": candidate.rvol,
                "float_shares": candidate.float_shares,
                "impact": _impact(candidate.rvol),
            }
            logger.info(
                "GNW MOMENTUM ALERT %s +%.1f%% RVOL %.1fx (was pending) | %s",
                symbol, candidate.gap_pct, candidate.rvol, pdata["headline"],
            )
            if self._on_alert is not None:
                await self._on_alert(alert)

        try:
            items = await asyncio.to_thread(self._fetch_items)
        except Exception as exc:
            logger.warning("GlobeNewswire fetch error: %s", exc)
            return

        for item in items:
            guid = item["guid"]
            if guid in self._seen_guids:
                continue
            self._seen_guids.add(guid)

            if len(self._seen_guids) > self._gnw_cfg.max_seen_guids:
                self._seen_guids.clear()
                self._seen_guids.add(guid)

            if not self._is_today(item["pub_date"]):
                continue

            headline = item["title"]
            for symbol in self._extract_symbols(headline):
                if symbol in self._alerted or symbol in self._pending:
                    continue
                candidate, only_momentum_failed = await asyncio.to_thread(
                    self._scanner._check_symbol_with_detail, symbol
                )
                if candidate is None:
                    if only_momentum_failed:
                        self._pending[symbol] = {
                            "headline": headline,
                            "source": "GlobeNewswire",
                            "added_at": time.time(),
                        }
                        logger.info(
                            "GNW PENDING %s — static filters passed, watching gap%%/RVOL | %s",
                            symbol, headline,
                        )
                    else:
                        logger.debug("GNW %s: failed static filters — skip", symbol)
                    continue

                self._alerted.add(symbol)
                alert = {
                    "id": f"gnw_{symbol}_{int(time.time())}",
                    "time": _et_now(),
                    "symbol": symbol,
                    "headline": headline,
                    "source": "GlobeNewswire",
                    "price": candidate.price,
                    "gap_pct": candidate.gap_pct,
                    "rvol": candidate.rvol,
                    "float_shares": candidate.float_shares,
                    "impact": _impact(candidate.rvol),
                }
                logger.info(
                    "GNW ALERT %s +%.1f%% RVOL %.1fx | %s",
                    symbol, candidate.gap_pct, candidate.rvol, headline,
                )
                if self._on_alert is not None:
                    await self._on_alert(alert)

    async def run(self) -> None:
        if not self._gnw_cfg.enabled:
            return
        logger.info(
            "GlobeNewswire poller started (interval=%ds, url=%s)",
            self._gnw_cfg.poll_interval_seconds,
            self._gnw_cfg.feed_url,
        )
        while True:
            await self._poll_once()
            await asyncio.sleep(self._gnw_cfg.poll_interval_seconds)


# ------------------------------------------------------------------ #
# CLI                                                                 #
# ------------------------------------------------------------------ #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Real-time Alpaca news stream — alerts the moment a gapper headline arrives.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--auto-trade",
        action="store_true",
        help="Execute a paper trade automatically when a qualifying alert fires",
    )
    args = parser.parse_args()

    config = Config()
    setup_logging(config.log_dir)

    bot = NewsStreamBot(config, auto_trade=args.auto_trade)
    try:
        bot.run()
    except KeyboardInterrupt:
        print("\nStream stopped.")


if __name__ == "__main__":
    main()
