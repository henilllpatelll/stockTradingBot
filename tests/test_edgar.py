"""Tests for edgar.py pure logic — no HTTP calls."""
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from edgar import EdgarSummary, EdgarClient, Filing8K, summarize


def _filing(items: str, filed_date: str = "2024-01-15") -> Filing8K:
    return Filing8K(
        symbol="TEST",
        filed_date=filed_date,
        filed_dt=datetime.fromisoformat(filed_date).replace(tzinfo=timezone.utc),
        accession="0001234567-24-000001",
        form_type="8-K",
        primary_doc="form8k.htm",
        items=items,
        document_url="https://example.com/form8k.htm",
    )


def _client_with_filings(filings):
    client = object.__new__(EdgarClient)
    client._last_req = 0.0
    client._cik_map = {}
    client._cik_loaded = False
    client.get_recent_8k = MagicMock(return_value=filings)
    client.get_recent_form4 = MagicMock(return_value=[])
    client.get_institutional_holders = MagicMock(return_value=[])
    return client


class TestEdgarSummaryDefaults:
    def test_no_risk_by_default(self):
        s = EdgarSummary(symbol="TEST")
        assert s.has_dilution_risk is False
        assert s.has_positive_catalyst is False

    def test_empty_lists_by_default(self):
        s = EdgarSummary(symbol="TEST")
        assert s.summary_lines == []
        assert s.recent_8k == []
        assert s.insider_trades == []
        assert s.institutional_holders == []


class TestEdgarClientSummarize:
    def test_dilution_item_sets_risk_flag(self):
        client = _client_with_filings([_filing("3.02")])
        s = client.summarize("TEST")
        assert s.has_dilution_risk is True
        assert s.has_positive_catalyst is False
        assert any("DILUTION" in line for line in s.summary_lines)

    def test_item_301_sets_dilution_flag(self):
        client = _client_with_filings([_filing("3.01")])
        s = client.summarize("TEST")
        assert s.has_dilution_risk is True

    def test_positive_item_101(self):
        client = _client_with_filings([_filing("1.01")])
        s = client.summarize("TEST")
        assert s.has_positive_catalyst is True
        assert s.has_dilution_risk is False
        assert any("CATALYST" in line for line in s.summary_lines)

    def test_earnings_item_202(self):
        client = _client_with_filings([_filing("2.02")])
        s = client.summarize("TEST")
        assert s.has_positive_catalyst is True

    def test_acquisition_item_201(self):
        client = _client_with_filings([_filing("2.01")])
        s = client.summarize("TEST")
        assert s.has_positive_catalyst is True

    def test_neutral_item_no_flags(self):
        client = _client_with_filings([_filing("5.02")])
        s = client.summarize("TEST")
        assert s.has_dilution_risk is False
        assert s.has_positive_catalyst is False

    def test_mixed_items_dilution_wins(self):
        client = _client_with_filings([_filing("1.01,3.02")])
        s = client.summarize("TEST")
        assert s.has_dilution_risk is True

    def test_no_filings_no_flags(self):
        client = _client_with_filings([])
        s = client.summarize("TEST")
        assert s.has_dilution_risk is False
        assert s.has_positive_catalyst is False
        assert s.summary_lines == []

    def test_summary_line_written_per_filing(self):
        client = _client_with_filings([_filing("1.01"), _filing("5.02")])
        s = client.summarize("TEST")
        assert len(s.summary_lines) == 2


class TestModuleLevelSummarize:
    def test_returns_empty_summary_on_any_error(self):
        with patch("edgar._default_client", None):
            with patch("edgar.EdgarClient") as MockClient:
                MockClient.return_value.summarize.side_effect = Exception("network error")
                result = summarize("FAIL")
        assert isinstance(result, EdgarSummary)
        assert result.symbol == "FAIL"
        assert result.has_dilution_risk is False
