"""Tests for alpaca_client.py — order price helpers, spread gate, get_news error handling."""

import pytest
from unittest.mock import MagicMock, patch
from alpaca.trading.enums import OrderSide

from alpaca_client import AlpacaClient


class TestAlpacaClientOrderOffsets:
    def _client_with_spy(self, monkeypatch):
        client = object.__new__(AlpacaClient)
        submitted = {}

        def fake_place_limit_order(symbol, qty, side, limit_price):
            submitted.update(
                {
                    "symbol": symbol,
                    "qty": qty,
                    "side": side,
                    "limit_price": limit_price,
                }
            )
            return submitted

        monkeypatch.setattr(client, "place_limit_order", fake_place_limit_order)
        return client, submitted

    def test_entry_limit_uses_ask_plus_ten_cents(self, monkeypatch):
        client, submitted = self._client_with_spy(monkeypatch)

        order = client.place_entry_limit_order("AAPL", 25, ask=10.01)

        assert order is submitted
        assert submitted["side"] == OrderSide.BUY
        assert submitted["limit_price"] == pytest.approx(10.11)

    def test_exit_limit_uses_bid_minus_ten_cents(self, monkeypatch):
        client, submitted = self._client_with_spy(monkeypatch)

        order = client.place_exit_limit_order("AAPL", 25, bid=10.01)

        assert order is submitted
        assert submitted["side"] == OrderSide.SELL
        assert submitted["limit_price"] == pytest.approx(9.91)


class TestGetQuoteWithSpread:
    def _client_with_quote(self, ask, bid, monkeypatch):
        client = object.__new__(AlpacaClient)
        monkeypatch.setattr(client, "get_latest_quote",
                            lambda sym: {"ask": ask, "bid": bid, "mid": (ask + bid) / 2})
        return client

    def test_spread_cents_calculated(self, monkeypatch):
        client = self._client_with_quote(10.20, 10.00, monkeypatch)
        result = client.get_quote_with_spread("AAPL")
        assert result["spread_cents"] == pytest.approx(20.0, abs=0.01)

    def test_spread_pct_calculated(self, monkeypatch):
        client = self._client_with_quote(10.20, 10.00, monkeypatch)
        result = client.get_quote_with_spread("AAPL")
        mid = 10.10
        assert result["spread_pct"] == pytest.approx((10.20 - 10.00) / mid * 100, abs=0.01)

    def test_wide_spread_flag_above_2pct(self, monkeypatch):
        client = self._client_with_quote(11.00, 10.00, monkeypatch)
        assert client.get_quote_with_spread("AAPL")["wide_spread"] is True

    def test_narrow_spread_flag_clear(self, monkeypatch):
        client = self._client_with_quote(10.05, 10.00, monkeypatch)
        assert client.get_quote_with_spread("AAPL")["wide_spread"] is False

    def test_exactly_2pct_is_not_wide(self, monkeypatch):
        # wide_spread = spread_pct > 2.0, so exactly 2.0 is not wide
        ask, bid = 10.2010, 9.9990   # spread = 0.202 / mid(10.10) = exactly 2.0%
        client = self._client_with_quote(ask, bid, monkeypatch)
        result = client.get_quote_with_spread("AAPL")
        assert result["wide_spread"] is False


class TestGetNews:
    def _client(self):
        client = object.__new__(AlpacaClient)
        client._news_headers = {"APCA-API-KEY-ID": "k", "APCA-API-SECRET-KEY": "s"}
        return client

    def test_returns_headlines_on_success(self):
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "news": [{"headline": "Earnings beat"}, {"headline": "Revenue up"}]
        }
        with patch("alpaca_client.requests.get", return_value=mock_resp):
            result = client.get_news("AAPL", lookback_hours=12)
        assert result == ["Earnings beat", "Revenue up"]

    def test_returns_empty_on_http_error(self):
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("403 Forbidden")
        with patch("alpaca_client.requests.get", return_value=mock_resp):
            result = client.get_news("AAPL", lookback_hours=12)
        assert result == []

    def test_returns_empty_on_network_error(self):
        client = self._client()
        with patch("alpaca_client.requests.get", side_effect=Exception("Timeout")):
            result = client.get_news("AAPL", lookback_hours=12)
        assert result == []

    def test_returns_empty_on_empty_news_array(self):
        client = self._client()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"news": []}
        with patch("alpaca_client.requests.get", return_value=mock_resp):
            result = client.get_news("AAPL", lookback_hours=12)
        assert result == []
