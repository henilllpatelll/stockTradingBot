"""
api.py — FastAPI backend.

Starts the real-time news stream and premarket scanner as background tasks
on startup. Serves live data via REST and WebSocket.

Run:
    python api.py
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime
from typing import Set

import pytz
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from alpaca_client import AlpacaClient
from catalyst_agent import get_analysis_data
from config import Config
from news_stream import NewsStreamBot
from scanner import CatalystChecker, PremarketScanner

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("api")

_ET = pytz.timezone("America/New_York")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Shared state --------------------------------------------------------- #
_cfg = Config()
_client = AlpacaClient(_cfg)
_scanner = PremarketScanner(_cfg, _client)
_checker = CatalystChecker(_cfg.scanner, _client)

_news_alerts: deque = deque(maxlen=100)
_scanner_candidates: list = []
_ws_clients: Set[WebSocket] = set()
_last_scan_time: str = "—"


# ---- WebSocket broadcast -------------------------------------------------- #

async def _broadcast(data: dict) -> None:
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    _ws_clients -= dead


# ---- Background: news stream ---------------------------------------------- #

async def _on_news_alert(alert: dict) -> None:
    _news_alerts.appendleft(alert)
    await _broadcast({"type": "alert", **alert})


async def _news_stream_task() -> None:
    bot = NewsStreamBot(_cfg, on_alert=_on_news_alert)
    await bot._run_with_reconnect()


# ---- Background: scanner loop --------------------------------------------- #

async def _scanner_task() -> None:
    global _scanner_candidates, _last_scan_time
    while True:
        try:
            universe = await asyncio.to_thread(_client.get_market_universe)
            if universe:
                candidates = await asyncio.to_thread(_scanner.scan_once, universe)
                results = []
                for c in candidates:
                    headlines = await asyncio.to_thread(
                        _checker.fetch_headlines, c.symbol, _cfg.scanner.news_lookback_hours
                    )
                    has_cat, headline = await asyncio.to_thread(
                        _checker.has_catalyst, c.symbol, headlines
                    )
                    results.append({
                        "symbol": c.symbol,
                        "price": c.price,
                        "gap_pct": c.gap_pct,
                        "rvol": c.rvol,
                        "float_shares": c.float_shares,
                        "market_cap": c.market_cap,
                        "catalyst": headline or "No catalyst found",
                        "verdict": "GO" if has_cat else "NO-GO",
                        "scan_time": c.scan_time,
                    })
                _scanner_candidates = results
                _last_scan_time = datetime.now(_ET).strftime("%H:%M:%S ET")
                logger.info("Scanner: %d candidates after catalyst check", len(results))
        except Exception as exc:
            logger.warning("Scanner task error: %s", exc)
        await asyncio.sleep(30)


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(_news_stream_task())
    asyncio.create_task(_scanner_task())
    logger.info("News stream and scanner tasks started")


# ---- Endpoints ------------------------------------------------------------ #

@app.get("/api/analysis")
def run_analysis():
    return get_analysis_data()


@app.get("/api/scanner")
def get_scanner():
    return {
        "status": "scanning",
        "last_updated": _last_scan_time,
        "candidates": _scanner_candidates,
    }


@app.get("/api/news")
def get_news():
    return {"status": "live", "alerts": list(_news_alerts)}


@app.websocket("/ws/news")
async def ws_news(websocket: WebSocket) -> None:
    await websocket.accept()
    _ws_clients.add(websocket)
    await websocket.send_json({"type": "history", "alerts": list(_news_alerts)})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        _ws_clients.discard(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
