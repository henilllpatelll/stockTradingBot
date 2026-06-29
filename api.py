"""
api.py — FastAPI backend.

Starts the real-time news stream as a background task on startup.
Serves live data via REST and WebSocket.

Run:
    python api.py
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Set

import pytz
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from alpaca_client import AlpacaClient
from config import Config
from news_stream import GlobeNewswirePoller, NewsStreamBot

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("api")

_ET = pytz.timezone("America/New_York")

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_news_stream_task())
    asyncio.create_task(_globenewswire_task())
    asyncio.create_task(_technical_scanner_task())
    logger.info("News stream, GlobeNewswire poller, and technical scanner started")
    yield


app = FastAPI(lifespan=lifespan)
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

_news_alerts: deque = deque(maxlen=100)
_technical_candidates: deque = deque(maxlen=100)
_ws_clients: Set[WebSocket] = set()
_active_trades: set[str] = set()

# Autonomous mode state
_auto_mode: bool = True
_traded_today: set[str] = set()
_traded_today_date: str = ""  # ET date string, e.g. "2026-05-18"

# Technical scanner dedup state (reset daily)
_scanner_seen: set[str] = set()
_scanner_seen_date: str = ""



class TradeRequest(BaseModel):
    symbol: str
    skip_filters: bool = True  # scanner/news candidates already passed premarket filters
    headline: str = ""
    strategy: str = "catalyst"


class AutoModeRequest(BaseModel):
    enabled: bool


# ---- WebSocket broadcast -------------------------------------------------- #

async def _broadcast(data: dict) -> None:
    if not _ws_clients:
        return
    dead = set()
    for ws in list(_ws_clients):
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


def _impact(rvol: float) -> str:
    if rvol >= 10:
        return "High"
    if rvol >= 5:
        return "Medium"
    return "Low"


# ---- Background: news stream ---------------------------------------------- #

async def _on_news_alert(alert: dict) -> None:
    _news_alerts.appendleft(alert)
    await _broadcast({"type": "alert", **alert})
    if _auto_mode and _is_trading_window() and _cfg.active_strategy in ("catalyst", "both"):
        sym = alert["symbol"]
        if sym not in _active_trades:
            logger.info("Auto-mode (news stream): queuing %s | %s", sym, alert.get("headline", ""))
            await _broadcast({"type": "auto_trade", "symbol": sym, "message": f"Auto-mode queuing {sym}"})
            asyncio.create_task(_auto_trade(sym, headline=alert.get("headline", "")))


async def _news_stream_task() -> None:
    bot = NewsStreamBot(_cfg, on_alert=_on_news_alert)
    await bot._run_with_reconnect()


async def _globenewswire_task() -> None:
    poller = GlobeNewswirePoller(_cfg, on_alert=_on_news_alert)
    await poller.run()


def _reset_traded_today_if_new_day() -> None:
    global _traded_today, _traded_today_date
    today = datetime.now(_ET).strftime("%Y-%m-%d")
    if today != _traded_today_date:
        _traded_today = set()
        _traded_today_date = today
        logger.info("Auto-mode: traded_today reset for %s", today)


def _is_trading_window() -> bool:
    now = datetime.now(_ET)
    return _cfg.trading_start_hour_et <= now.hour < _cfg.trading_end_hour_et


async def _auto_trade(symbol: str, strategy: str = "catalyst", headline: str = "") -> None:
    _active_trades.add(symbol)
    try:
        from run_bot import _AutoArgs, run
        args = _AutoArgs(
            no_trading_hours_gate=True,
            no_catalyst_check=strategy == "technical",
            catalyst_headline=headline,
        )
        logger.info("Auto-mode (%s): firing trade for %s", strategy, symbol)
        await asyncio.to_thread(run, symbol, _cfg, True, True, args, strategy)
        logger.info("Auto-mode (%s): trade complete for %s", strategy, symbol)
    except Exception as exc:
        logger.error("Auto-mode trade error %s: %s", symbol, exc)
    finally:
        _active_trades.discard(symbol)


# ---- Background: technical scanner (Strategy B) --------------------------- #

async def _on_technical_candidate(candidate) -> None:
    global _scanner_seen, _scanner_seen_date
    alert = {
        "id": f"tech_{candidate.symbol}_{int(time.time())}",
        "time": candidate.scan_time,
        "symbol": candidate.symbol,
        "price": candidate.price,
        "gap_pct": candidate.gap_pct,
        "rvol": candidate.rvol,
        "float_shares": candidate.float_shares,
        "source": "scanner",
        "headline": "",
        "impact": _impact(candidate.rvol),
    }
    _technical_candidates.appendleft(alert)
    await _broadcast({"type": "scanner_candidate", **alert})
    if _auto_mode and _is_trading_window() and _cfg.active_strategy in ("technical", "both"):
        sym = candidate.symbol
        if sym not in _active_trades:
            logger.info("Auto-mode (technical scanner): queuing %s", sym)
            await _broadcast({"type": "auto_trade", "symbol": sym,
                               "message": f"Auto-mode (technical) queuing {sym}"})
            asyncio.create_task(_auto_trade(sym, strategy="technical"))


async def _technical_scanner_task() -> None:
    from scanner import PremarketScanner, is_scanner_hours
    global _scanner_seen, _scanner_seen_date
    scanner = PremarketScanner(_cfg, _client)
    pending_symbols: set[str] = set()
    while True:
        today = datetime.now(_ET).strftime("%Y-%m-%d")
        if today != _scanner_seen_date:
            _scanner_seen = set()
            _scanner_seen_date = today
            pending_symbols.clear()
        if is_scanner_hours(_cfg):
            try:
                universe = await asyncio.to_thread(_client.get_market_universe)
                candidates, new_pending = await asyncio.to_thread(
                    scanner.scan_once_with_detail, universe, _cfg.technical_filters
                )
                candidate_symbols = {c.symbol for c in candidates}
                for sym in new_pending:
                    if sym not in _scanner_seen and sym not in candidate_symbols:
                        pending_symbols.add(sym)
                pending_symbols -= candidate_symbols
                for sym in list(pending_symbols):
                    promoted = await asyncio.to_thread(scanner.recheck_momentum, sym, _cfg.technical_filters)
                    if promoted is not None:
                        pending_symbols.discard(sym)
                        candidates.append(promoted)
                for c in candidates:
                    if c.symbol not in _scanner_seen:
                        _scanner_seen.add(c.symbol)
                        await _on_technical_candidate(c)
            except Exception as exc:
                logger.warning("Technical scanner error: %s", exc)
        await asyncio.sleep(_cfg.scanner.refresh_interval_seconds)



# ---- Endpoints ------------------------------------------------------------ #

@app.get("/api/scanner")
def get_scanner():
    return {"status": "live", "candidates": list(_technical_candidates)}


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


@app.post("/api/trade")
async def place_trade(req: TradeRequest) -> dict:
    symbol = req.symbol.strip().upper()
    if not symbol.isalpha() or not (1 <= len(symbol) <= 5):
        raise HTTPException(status_code=422, detail=f"Invalid symbol: {req.symbol!r}")
    if symbol in _active_trades:
        return {"status": "busy", "message": f"{symbol} trade already in progress"}

    async def _run() -> None:
        _active_trades.add(symbol)
        try:
            from run_bot import _AutoArgs, run
            strategy = req.strategy if req.strategy in ("catalyst", "technical") else "catalyst"
            args = _AutoArgs(
                no_trading_hours_gate=True,
                no_catalyst_check=strategy == "technical",
                catalyst_headline=req.headline,
            )
            await asyncio.to_thread(run, symbol, _cfg, req.skip_filters, True, args, strategy)
        except Exception as exc:
            logger.error("Trade error %s: %s", symbol, exc)
        finally:
            _active_trades.discard(symbol)

    asyncio.create_task(_run())
    return {"status": "ok", "message": f"Trade queued for {symbol}"}


@app.get("/api/positions")
def get_positions() -> dict:
    try:
        positions = _client._trading.get_all_positions()
        return {"positions": [
            {
                "symbol": p.symbol,
                "qty": int(p.qty),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
                "current_price": float(p.current_price),
                "avg_entry_price": float(p.avg_entry_price),
            }
            for p in positions
        ]}
    except Exception as exc:
        logger.warning("positions fetch failed: %s", exc)
        return {"positions": []}


@app.get("/api/active-trades")
def get_active_trades() -> dict:
    return {"active": list(_active_trades)}


@app.get("/api/auto-mode")
def get_auto_mode() -> dict:
    return {
        "enabled": _auto_mode,
        "traded_today": sorted(_traded_today),
        "trading_window": _is_trading_window(),
    }


@app.post("/api/auto-mode")
async def set_auto_mode(req: AutoModeRequest) -> dict:
    global _auto_mode
    _auto_mode = req.enabled
    logger.info("Auto-mode: %s", "ENABLED" if _auto_mode else "DISABLED")
    await _broadcast({"type": "auto_mode", "enabled": _auto_mode})
    return {"enabled": _auto_mode}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=False)
