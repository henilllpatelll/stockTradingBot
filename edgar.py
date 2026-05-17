"""
SEC EDGAR integration — 8-K filings, Form 4 insider trades, 13F institutional holders.
No API key required. Rate limit: ~10 req/sec enforced via 0.12s delay.
SEC requires User-Agent header: https://www.sec.gov/privacy.htm#edgaraccess
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

_HEADERS = {
    "User-Agent": "stock-trading-bot henillpatel2004@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}
_BASE_DATA = "https://data.sec.gov"
_BASE_EFTS = "https://efts.sec.gov"
_BASE_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
_MIN_DELAY = 0.12  # Stay safely under 10 req/sec


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class Filing8K:
    symbol: str
    filed_date: str          # YYYY-MM-DD
    filed_dt: datetime
    accession: str           # e.g. "0001234567-24-000001"
    form_type: str           # "8-K" or "8-K/A"
    primary_doc: str         # primary document filename
    items: str               # item codes, e.g. "1.01,5.02"
    document_url: str


@dataclass
class InsiderTrade:
    symbol: str
    filed_date: str
    filed_dt: datetime
    accession: str
    insider_name: str
    title: str
    transaction_code: str    # P=purchase S=sale A=award D=disposition
    shares: float
    price_per_share: float
    total_value: float


@dataclass
class InstitutionalHolder:
    institution_name: str
    cik: str
    period_of_report: str
    filing_url: str


@dataclass
class EdgarSummary:
    symbol: str
    recent_8k: list[Filing8K] = field(default_factory=list)
    insider_trades: list[InsiderTrade] = field(default_factory=list)
    institutional_holders: list[InstitutionalHolder] = field(default_factory=list)
    has_dilution_risk: bool = False
    has_positive_catalyst: bool = False
    summary_lines: list[str] = field(default_factory=list)


# ── 8-K item labels and risk classification ───────────────────────────────────

_ITEM_LABELS: dict[str, str] = {
    "1.01": "Entry into Material Agreement",
    "1.02": "Termination of Material Agreement",
    "1.03": "Bankruptcy or Receivership",
    "2.01": "Completion of Acquisition/Disposition",
    "2.02": "Results of Operations / Earnings",
    "2.03": "Creation of Direct Financial Obligation",
    "2.06": "Material Impairments",
    "3.01": "Delisting of Issuer's Securities",
    "3.02": "Unregistered Sales of Securities",  # dilution
    "4.01": "Changes in Auditor",
    "5.01": "Changes in Control",
    "5.02": "Departure/Election of Officers or Directors",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

_DILUTION_ITEMS = {"3.01", "3.02"}
_POSITIVE_ITEMS = {"1.01", "2.01", "2.02"}

_TX_LABELS: dict[str, str] = {
    "P": "BOUGHT",
    "S": "SOLD",
    "A": "AWARDED",
    "D": "DISPOSED",
    "G": "GIFTED",
    "M": "OPTION EXERCISED",
    "F": "TAX WITHHELD",
}


# ── EDGAR client ──────────────────────────────────────────────────────────────

class EdgarClient:
    """
    SEC EDGAR API client.
    Fetches 8-K, Form 4, and 13F data — no auth required, free public API.
    """

    def __init__(self) -> None:
        self._last_req: float = 0.0
        self._cik_map: dict[str, str] = {}
        self._cik_loaded: bool = False

    # ── HTTP helpers ─────────────────────────────────────────────────────────

    def _get_json(self, url: str, params: dict | None = None) -> dict | None:
        elapsed = time.monotonic() - self._last_req
        if elapsed < _MIN_DELAY:
            time.sleep(_MIN_DELAY - elapsed)
        try:
            r = requests.get(url, headers=_HEADERS, params=params, timeout=15)
            self._last_req = time.monotonic()
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def _get_text(self, url: str) -> str | None:
        elapsed = time.monotonic() - self._last_req
        if elapsed < _MIN_DELAY:
            time.sleep(_MIN_DELAY - elapsed)
        try:
            r = requests.get(url, headers=_HEADERS, timeout=15)
            self._last_req = time.monotonic()
            r.raise_for_status()
            return r.text
        except Exception:
            return None

    # ── CIK lookup ───────────────────────────────────────────────────────────

    def _load_cik_map(self) -> None:
        if self._cik_loaded:
            return
        data = self._get_json("https://www.sec.gov/files/company_tickers.json")
        if data:
            for entry in data.values():
                ticker = str(entry.get("ticker", "")).upper()
                cik = str(entry.get("cik_str", "")).zfill(10)
                if ticker:
                    self._cik_map[ticker] = cik
        self._cik_loaded = True

    def get_cik(self, ticker: str) -> str | None:
        """Return zero-padded 10-digit CIK for ticker, or None if not in EDGAR."""
        self._load_cik_map()
        return self._cik_map.get(ticker.upper())

    # ── 8-K filings ──────────────────────────────────────────────────────────

    def get_recent_8k(self, ticker: str, lookback_hours: int = 48) -> list[Filing8K]:
        """
        8-K and 8-K/A filings from the last lookback_hours, newest first.
        Includes item codes (e.g. "1.01,5.02") for catalyst classification.
        """
        cik = self.get_cik(ticker)
        if not cik:
            return []
        subs = self._get_json(f"{_BASE_DATA}/submissions/CIK{cik}.json")
        if not subs:
            return []

        recent = subs.get("filings", {}).get("recent", {})
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        results: list[Filing8K] = []

        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        items_arr = recent.get("items", [])

        for form, date_str, accession, doc, items in zip(forms, dates, accessions, docs, items_arr):
            if form not in ("8-K", "8-K/A"):
                continue
            try:
                filed_dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if filed_dt < cutoff:
                break  # submissions are newest-first; nothing older qualifies

            cik_int = int(cik)
            accession_clean = accession.replace("-", "")
            doc_url = f"{_BASE_ARCHIVES}/{cik_int}/{accession_clean}/{doc}"
            results.append(Filing8K(
                symbol=ticker.upper(),
                filed_date=date_str,
                filed_dt=filed_dt,
                accession=accession,
                form_type=form,
                primary_doc=doc,
                items=items or "",
                document_url=doc_url,
            ))

        return results

    # ── Form 4 insider trades ─────────────────────────────────────────────────

    def get_recent_form4(self, ticker: str, lookback_hours: int = 168) -> list[InsiderTrade]:
        """
        Form 4 insider trade filings for ticker, default 7-day lookback.
        Parses each filing's XML — returns [] on any error.
        """
        cik = self.get_cik(ticker)
        if not cik:
            return []
        subs = self._get_json(f"{_BASE_DATA}/submissions/CIK{cik}.json")
        if not subs:
            return []

        recent = subs.get("filings", {}).get("recent", {})
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        results: list[InsiderTrade] = []

        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])

        for form, date_str, accession, doc in zip(forms, dates, accessions, docs):
            if form != "4":
                continue
            try:
                filed_dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if filed_dt < cutoff:
                break

            trade = self._parse_form4_xml(ticker, cik, accession, filed_dt, date_str, doc)
            if trade:
                results.append(trade)

        return results

    def _parse_form4_xml(
        self,
        ticker: str,
        cik: str,
        accession: str,
        filed_dt: datetime,
        date_str: str,
        doc_name: str,
    ) -> InsiderTrade | None:
        cik_int = int(cik)
        accession_clean = accession.replace("-", "")
        xml_text = self._get_text(f"{_BASE_ARCHIVES}/{cik_int}/{accession_clean}/{doc_name}")
        if not xml_text:
            return None
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return None

        def _txt(tag: str) -> str:
            el = root.find(f".//{tag}")
            return el.text.strip() if el is not None and el.text else ""

        insider_name = _txt("rptOwnerName") or "Unknown"
        title = _txt("officerTitle")
        if not title:
            if _txt("isOfficer") == "1":
                title = "Officer"
            elif _txt("isDirector") == "1":
                title = "Director"
            else:
                title = "Insider"

        transactions = root.findall(".//nonDerivativeTransaction")
        if not transactions:
            return None
        tx = transactions[0]

        def _tx(tag: str) -> str:
            el = tx.find(f".//{tag}")
            return el.text.strip() if el is not None and el.text else ""

        try:
            tx_code = _tx("transactionCode")
            shares = float(_tx("transactionShares") or 0)
            price = float(_tx("transactionPricePerShare") or 0)
        except (ValueError, TypeError):
            return None

        if shares == 0:
            return None

        return InsiderTrade(
            symbol=ticker.upper(),
            filed_date=date_str,
            filed_dt=filed_dt,
            accession=accession,
            insider_name=insider_name,
            title=title,
            transaction_code=tx_code,
            shares=shares,
            price_per_share=price,
            total_value=round(shares * price, 2),
        )

    # ── 13F institutional holders ─────────────────────────────────────────────

    def get_institutional_holders(self, ticker: str, max_results: int = 15) -> list[InstitutionalHolder]:
        """
        EDGAR full-text search for 13F-HR filings that mention ticker.
        Returns institutions that filed recently — does not guarantee current holdings
        without parsing each 13F XML (a much heavier operation).
        """
        now = datetime.now()
        data = self._get_json(
            f"{_BASE_EFTS}/LATEST/search-index/",
            params={
                "q": f'"{ticker}"',
                "forms": "13F-HR",
                "dateRange": "custom",
                "startdt": (now - timedelta(days=200)).strftime("%Y-%m-%d"),
                "enddt": now.strftime("%Y-%m-%d"),
            },
        )
        if not data:
            return []

        results: list[InstitutionalHolder] = []
        for hit in data.get("hits", {}).get("hits", [])[:max_results]:
            src = hit.get("_source", {})
            names = src.get("display_names", [])
            name = names[0] if names else "Unknown Institution"
            entity_id = str(src.get("entity_id", ""))
            results.append(InstitutionalHolder(
                institution_name=name,
                cik=entity_id,
                period_of_report=src.get("period_of_report", ""),
                filing_url=(
                    f"https://www.sec.gov/cgi-bin/browse-edgar"
                    f"?action=getcompany&CIK={entity_id}&type=13F-HR&count=5"
                ),
            ))

        return results

    # ── Unified summary ───────────────────────────────────────────────────────

    def summarize(
        self,
        ticker: str,
        lookback_8k_hours: int = 48,
        lookback_form4_hours: int = 168,
    ) -> EdgarSummary:
        """
        Full EDGAR check: 8-K filings + Form 4 insider trades + 13F holders.
        Classifies 8-K items for dilution risk and positive catalyst signals.
        """
        s = EdgarSummary(symbol=ticker.upper())
        s.recent_8k = self.get_recent_8k(ticker, lookback_8k_hours)
        s.insider_trades = self.get_recent_form4(ticker, lookback_form4_hours)
        s.institutional_holders = self.get_institutional_holders(ticker)

        for f in s.recent_8k:
            codes = {c.strip() for c in f.items.split(",") if c.strip()}
            label = ", ".join(_ITEM_LABELS.get(c, f"Item {c}") for c in sorted(codes)) or f.primary_doc
            if codes & _DILUTION_ITEMS:
                s.has_dilution_risk = True
                s.summary_lines.append(f"[8-K DILUTION RISK] {f.filed_date}: Items {f.items} — {label}")
            elif codes & _POSITIVE_ITEMS:
                s.has_positive_catalyst = True
                s.summary_lines.append(f"[8-K CATALYST] {f.filed_date}: Items {f.items} — {label}")
            else:
                s.summary_lines.append(f"[8-K] {f.filed_date}: Items {f.items} — {label}")

        for t in s.insider_trades:
            action = _TX_LABELS.get(t.transaction_code, t.transaction_code)
            s.summary_lines.append(
                f"[INSIDER {action}] {t.filed_date}: {t.insider_name} ({t.title})"
                f" {t.shares:,.0f} sh @ ${t.price_per_share:.2f} = ${t.total_value:,.0f}"
            )

        if s.institutional_holders:
            names = ", ".join(h.institution_name for h in s.institutional_holders[:5])
            s.summary_lines.append(f"[13F] Recent 13F filers mentioning {ticker.upper()}: {names}")

        return s


# ── Module-level convenience ──────────────────────────────────────────────────

_default_client: Optional[EdgarClient] = None


def _client() -> EdgarClient:
    global _default_client
    if _default_client is None:
        _default_client = EdgarClient()
    return _default_client


def check_8k(ticker: str, lookback_hours: int = 48) -> list[Filing8K]:
    """Recent 8-K filings for ticker. Returns [] on any error."""
    try:
        return _client().get_recent_8k(ticker, lookback_hours)
    except Exception:
        return []


def check_insiders(ticker: str, lookback_hours: int = 168) -> list[InsiderTrade]:
    """Recent Form 4 insider trades for ticker. Returns [] on any error."""
    try:
        return _client().get_recent_form4(ticker, lookback_hours)
    except Exception:
        return []


def summarize(ticker: str) -> EdgarSummary:
    """Full EDGAR check: 8-K + Form 4 + 13F. Never raises."""
    try:
        return _client().summarize(ticker)
    except Exception:
        return EdgarSummary(symbol=ticker.upper())


# ── CLI quick-check ───────────────────────────────────────────────────────────

def _print_summary(ticker: str) -> None:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"\nEDGAR Summary — {ticker.upper()}")
    print("=" * 64)
    s = summarize(ticker)
    if not s.summary_lines:
        print("  No recent 8-K, Form 4, or 13F activity found.")
    for line in s.summary_lines:
        print(f"  {line}")
    if s.has_dilution_risk:
        print("\n  *** DILUTION RISK DETECTED ***")
    if s.has_positive_catalyst:
        print("\n  *** POSITIVE CATALYST DETECTED ***")
    print()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        for t in sys.argv[1:]:
            _print_summary(t)
    else:
        print("Usage: python edgar.py TICKER [TICKER ...]")
