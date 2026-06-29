from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import pytz
import requests
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockSnapshotRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
)

from config import Config

logger = logging.getLogger(__name__)

_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"


class AlpacaClient:
    """Thin wrapper around the Alpaca paper-trading and data APIs."""

    def __init__(self, config: Config) -> None:
        if not config.paper_only:
            raise ValueError("Only paper trading is supported.")
        config.alpaca.validate()
        self._api_key = config.alpaca.api_key
        self._secret_key = config.alpaca.secret_key
        self._trading = TradingClient(
            api_key=self._api_key,
            secret_key=self._secret_key,
            paper=True,
        )
        self._data = StockHistoricalDataClient(
            api_key=self._api_key,
            secret_key=self._secret_key,
        )
        self._news_headers = {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._secret_key,
        }

    # ------------------------------------------------------------------ #
    # Account                                                              #
    # ------------------------------------------------------------------ #

    def get_account(self):
        return self._trading.get_account()

    def get_equity(self) -> float:
        return float(self.get_account().equity)

    # ------------------------------------------------------------------ #
    # Market data                                                          #
    # ------------------------------------------------------------------ #

    def get_latest_quote(self, symbol: str) -> dict:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed="sip")
        quotes = self._data.get_stock_latest_quote(req)
        q = quotes[symbol]
        ask = float(q.ask_price)
        bid = float(q.bid_price)
        return {"ask": ask, "bid": bid, "mid": (ask + bid) / 2}

    def get_quote_with_spread(self, symbol: str) -> dict:
        q = self.get_latest_quote(symbol)
        ask, bid, mid = q["ask"], q["bid"], q["mid"]
        spread_cents = round((ask - bid) * 100, 2)
        spread_pct = round((ask - bid) / mid * 100, 2) if mid > 0 else 0.0
        return {
            "ask": ask,
            "bid": bid,
            "mid": mid,
            "spread_cents": spread_cents,
            "spread_pct": spread_pct,
            "wide_spread": spread_pct > 2.0,
        }

    def get_premarket_bars(self, symbol: str, date: Optional[datetime] = None) -> pd.DataFrame:
        """Return 1-min bars from 4:00 AM to 9:29 AM ET for the given date (default: today)."""
        et_tz = pytz.timezone("America/New_York")
        if date is None:
            now_et = datetime.now(et_tz)
        else:
            now_et = date.astimezone(et_tz) if date.tzinfo else et_tz.localize(date)
        trading_date = now_et.date()
        start_et = et_tz.localize(datetime(trading_date.year, trading_date.month, trading_date.day, 4, 0, 0))
        end_et = et_tz.localize(datetime(trading_date.year, trading_date.month, trading_date.day, 9, 29, 59))
        start_utc = start_et.astimezone(timezone.utc)
        end_utc = end_et.astimezone(timezone.utc)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start_utc,
            end=end_utc,
            feed="sip",
        )
        bars = self._data.get_stock_bars(req)
        return self._to_df(bars, symbol)

    def get_snapshot(self, symbol: str):
        req = StockSnapshotRequest(symbol_or_symbols=symbol, feed="sip")
        snapshots = self._data.get_stock_snapshot(req)
        return snapshots.get(symbol)

    def get_daily_bars(self, symbol: str, days: int = 20) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        # Add calendar buffer for weekends / holidays.
        start = end - timedelta(days=days + 14)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed="sip",
        )
        bars = self._data.get_stock_bars(req)
        return self._to_df(bars, symbol).tail(days)

    def get_intraday_bars(
        self,
        symbol: str,
        timeframe: TimeFrame = TimeFrame.Minute,
        lookback_hours: int = 6,
    ) -> pd.DataFrame:
        """Return minute bars covering the last *lookback_hours* hours (includes premarket)."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=lookback_hours)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            feed="sip",
        )
        bars = self._data.get_stock_bars(req)
        return self._to_df(bars, symbol)

    def get_market_universe(self, max_symbols: int = 100) -> list[str]:
        """Return active small-cap ticker symbols using Alpaca's screener API."""
        symbols: list[str] = []
        headers = {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._secret_key,
        }
        screens = [
            ("https://data.alpaca.markets/v1beta1/screener/stocks/most-actives?by=volume&top=50", "most_actives"),
            ("https://data.alpaca.markets/v1beta1/screener/stocks/movers?top=50", "gainers"),
        ]
        for url, key in screens:
            try:
                resp = requests.get(url, headers=headers, timeout=10)
                resp.raise_for_status()
                for entry in resp.json().get(key, []):
                    sym = entry.get("symbol", "")
                    if sym and sym.isalpha() and sym not in symbols:
                        symbols.append(sym)
            except Exception as exc:
                logger.warning("get_market_universe screen=%s failed: %s", key, exc)
        if not symbols:
            logger.error("get_market_universe returned no symbols")
        return symbols[:max_symbols]

    def get_news(self, symbol: str, lookback_hours: int = 24) -> list[str]:
        """Return headline strings for *symbol* from the last *lookback_hours* hours.
        Returns [] on any error — callers should never have to catch."""
        start = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        try:
            resp = requests.get(
                _NEWS_URL,
                headers=self._news_headers,
                params={"symbols": symbol, "start": start, "limit": 10, "sort": "desc"},
                timeout=5,
            )
            resp.raise_for_status()
            return [item["headline"] for item in resp.json().get("news", [])]
        except Exception:
            return []

    @staticmethod
    def _to_df(bars_response, symbol: str) -> pd.DataFrame:
        df = bars_response.df
        if df.empty:
            return df
        if isinstance(df.index, pd.MultiIndex):
            try:
                df = df.loc[symbol]
            except KeyError:
                return pd.DataFrame()
        return df

    # ------------------------------------------------------------------ #
    # Order management                                                     #
    # ------------------------------------------------------------------ #

    def place_entry_limit_order(self, symbol: str, qty: int, ask: float):
        """Buy limit at ask + $0.10."""
        return self.place_limit_order(symbol, qty, OrderSide.BUY, round(ask + 0.10, 2))

    def place_exit_limit_order(self, symbol: str, qty: int, bid: float):
        """Sell limit at bid - $0.10."""
        return self.place_limit_order(symbol, qty, OrderSide.SELL, round(bid - 0.10, 2))

    def place_market_order(self, symbol: str, qty: int, side: OrderSide):
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
        )
        order = self._trading.submit_order(req)
        logger.info(
            "Market order submitted: %s %d %s — id=%s",
            side.value,
            qty,
            symbol,
            order.id,
        )
        return order

    def place_limit_order(self, symbol: str, qty: int, side: OrderSide, limit_price: float):
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 2),
        )
        order = self._trading.submit_order(req)
        logger.info(
            "Limit order submitted: %s %d %s @ $%.2f — id=%s",
            side.value,
            qty,
            symbol,
            limit_price,
            order.id,
        )
        return order

    def get_order(self, order_id: str) -> Optional[object]:
        try:
            return self._trading.get_order_by_id(order_id)
        except Exception:
            return None

    def cancel_order(self, order_id: str) -> None:
        try:
            self._trading.cancel_order_by_id(order_id)
            logger.info("Order cancelled: %s", order_id)
        except Exception as exc:
            logger.warning("Could not cancel order %s: %s", order_id, exc)

    def get_position(self, symbol: str) -> Optional[object]:
        try:
            return self._trading.get_open_position(symbol)
        except Exception:
            return None

    def close_position(self, symbol: str) -> Optional[object]:
        try:
            result = self._trading.close_position(symbol)
            logger.info("Position closed: %s", symbol)
            return result
        except Exception as exc:
            logger.error("Failed to close %s: %s", symbol, exc)
            raise

    def cancel_orders_for_symbol(self, symbol: str) -> int:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = self._trading.get_orders(filter=req)
        cancelled = 0
        for order in orders:
            if order.symbol == symbol:
                try:
                    self._trading.cancel_order_by_id(order.id)
                    cancelled += 1
                except Exception as exc:
                    logger.warning("Could not cancel order %s: %s", order.id, exc)
        if cancelled:
            logger.info("Cancelled %d open orders for %s", cancelled, symbol)
        return cancelled
