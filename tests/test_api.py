"""Tests for api.py — FastAPI endpoints with mocked Alpaca client and background tasks."""
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture(scope="module")
def client():
    """Return a FastAPI TestClient with background tasks and AlpacaClient mocked out."""
    import api

    # Swap long-running background coroutines with no-ops before startup fires
    async def _noop():
        return

    original_news = api._news_stream_task
    api._news_stream_task = _noop

    # Mock Alpaca calls used by endpoints
    mock_positions = []
    api._client.get_all_positions = MagicMock(return_value=mock_positions)

    from fastapi.testclient import TestClient
    with TestClient(api.app) as c:
        yield c

    api._news_stream_task = original_news


class TestNewsEndpoint:
    def test_returns_200(self, client):
        r = client.get("/api/news")
        assert r.status_code == 200

    def test_schema(self, client):
        data = client.get("/api/news").json()
        assert "status" in data
        assert "alerts" in data
        assert isinstance(data["alerts"], list)

    def test_status_is_live(self, client):
        data = client.get("/api/news").json()
        assert data["status"] == "live"


class TestScannerEndpoint:
    def test_returns_200(self, client):
        r = client.get("/api/scanner")
        assert r.status_code == 200

    def test_schema(self, client):
        data = client.get("/api/scanner").json()
        assert "status" in data
        assert "candidates" in data
        assert isinstance(data["candidates"], list)


class TestActiveTradesEndpoint:
    def test_returns_200(self, client):
        r = client.get("/api/active-trades")
        assert r.status_code == 200

    def test_schema(self, client):
        data = client.get("/api/active-trades").json()
        assert "active" in data
        assert isinstance(data["active"], list)


class TestPositionsEndpoint:
    def test_returns_200(self, client):
        r = client.get("/api/positions")
        assert r.status_code == 200

    def test_positions_key_present(self, client):
        data = client.get("/api/positions").json()
        assert "positions" in data
        assert isinstance(data["positions"], list)


class TestTradeEndpoint:
    def test_valid_symbol_queues_trade(self, client):
        with patch("api.asyncio.create_task"):
            r = client.post("/api/trade", json={"symbol": "AAPL"})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "AAPL" in data["message"]

    def test_invalid_symbol_digits(self, client):
        r = client.post("/api/trade", json={"symbol": "AA1"})
        assert r.status_code == 422

    def test_invalid_symbol_too_long(self, client):
        r = client.post("/api/trade", json={"symbol": "TOOLONG"})
        assert r.status_code == 422

    def test_invalid_symbol_empty(self, client):
        r = client.post("/api/trade", json={"symbol": ""})
        assert r.status_code == 422

    def test_duplicate_trade_returns_busy(self, client):
        import api
        api._active_trades.add("TSLA")
        try:
            r = client.post("/api/trade", json={"symbol": "TSLA"})
            assert r.status_code == 200
            assert r.json()["status"] == "busy"
        finally:
            api._active_trades.discard("TSLA")

    def test_case_normalization(self, client):
        with patch("api.asyncio.create_task"):
            r = client.post("/api/trade", json={"symbol": "gme"})
        assert r.status_code == 200
        assert "GME" in r.json()["message"]


class TestAutoTrade:
    def test_news_headline_is_passed_to_run_bot(self):
        import api

        captured = {}

        async def fake_to_thread(func, *args):
            captured["func"] = func
            captured["args"] = args

        with patch("api.asyncio.to_thread", side_effect=fake_to_thread):
            asyncio.run(api._auto_trade("AAPL", headline="AAPL announces major contract"))

        run_args = captured["args"][4]
        assert run_args.catalyst_headline == "AAPL announces major contract"
        assert run_args.no_catalyst_check is False
