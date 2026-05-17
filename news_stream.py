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
import sys
import time
from datetime import datetime
from pathlib import Path

import pytz

from alpaca_client import AlpacaClient
from config import Config
from scanner import PremarketScanner

logger = logging.getLogger("news_stream")
_ET = pytz.timezone("America/New_York")

_WS_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
_RECONNECT_DELAY_S = 5
_MAX_RECONNECT_DELAY_S = 60


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


class NewsStreamBot:
    def __init__(self, config: Config, auto_trade: bool = False) -> None:
        self._cfg = config
        self._auto_trade = auto_trade
        self._client = AlpacaClient(config)
        self._scanner = PremarketScanner(config, self._client)
        self._alerted: set[str] = set()

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

        headline = event.get("headline", "")
        source = event.get("source", "")
        symbols: list[str] = event.get("symbols") or []

        for symbol in symbols:
            if not symbol or symbol in self._alerted:
                continue
            if not symbol.replace(".", "").isalpha():
                continue

            # Filter check in thread pool — avoids blocking the WS event loop
            candidate = await asyncio.to_thread(self._scanner._check_symbol, symbol)
            if candidate is None:
                logger.debug("%s: news arrived but failed filters — skip", symbol)
                continue

            self._alerted.add(symbol)
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
                delay = _RECONNECT_DELAY_S  # clean disconnect — reset backoff

    def run(self) -> None:
        asyncio.run(self._run_with_reconnect())


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
