"""Free, source-audited SEC research data collection and normalization."""
from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime
from html import unescape

import requests

HEADERS = {"User-Agent": "earnings-research-lab/1.0 research@example.com", "Accept-Encoding": "gzip, deflate"}
_lock = threading.Lock()
_last_request = 0.0
_ticker_map = None

CONCEPTS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "total_debt": ["LongTermDebtAndFinanceLeaseObligationsCurrent", "LongTermDebtCurrent", "LongTermDebtNoncurrent"],
    "assets": ["Assets"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
}


def _get(url: str, *, json_response=True):
    global _last_request
    with _lock:
        wait = 0.12 - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        response = requests.get(url, headers=HEADERS, timeout=20)
        _last_request = time.monotonic()
    response.raise_for_status()
    return response.json() if json_response else response.text


def cik_for_ticker(ticker: str) -> int | None:
    global _ticker_map
    if _ticker_map is None:
        payload = _get("https://www.sec.gov/files/company_tickers.json")
        _ticker_map = {row["ticker"].upper(): int(row["cik_str"]) for row in payload.values()}
    return _ticker_map.get(ticker.upper())


def _submission_rows(recent: dict, cik: int) -> list[dict]:
    rows=[]
    count=len(recent.get("form",[]))
    descriptions=recent.get("primaryDocDescription", [""]*count)
    for i, form in enumerate(recent.get("form", [])):
        if form not in {"10-K", "10-Q", "8-K", "6-K", "20-F", "40-F"}: continue
        accession=recent["accessionNumber"][i]; primary=recent["primaryDocument"][i]; compact=accession.replace("-","")
        rows.append({"form":form,"filed":recent["filingDate"][i],"report_date":recent["reportDate"][i],"accession":accession,"description":descriptions[i] if i<len(descriptions) else "","url":f"https://www.sec.gov/Archives/edgar/data/{cik}/{compact}/{primary}","index_url":f"https://www.sec.gov/Archives/edgar/data/{cik}/{compact}/{accession}-index.html"})
    return rows


def filings(ticker: str, limit=40, start_year: int | None = None) -> list[dict]:
    cik = cik_for_ticker(ticker)
    if not cik:
        return []
    payload = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    recent = payload.get("filings", {}).get("recent", {})
    rows = _submission_rows(recent, cik)
    if start_year is not None:
        for old in payload.get("filings", {}).get("files", []):
            old_from=int(str(old.get("filingFrom") or "9999")[:4]); old_to=int(str(old.get("filingTo") or "0")[:4])
            if old_to < start_year: continue
            shard=_get(f"https://data.sec.gov/submissions/{old['name']}")
            rows.extend(_submission_rows(shard, cik))
    rows=[r for r in rows if start_year is None or int(r["filed"][:4])>=start_year]
    unique={r["accession"]:r for r in rows}
    return sorted(unique.values(),key=lambda r:r["filed"],reverse=True)[:limit]


def _quarterly_facts(facts: dict, tags: list[str]) -> list[dict]:
    for tag in tags:
        node = facts.get("us-gaap", {}).get(tag)
        if not node:
            continue
        units = node.get("units", {})
        candidates = units.get("USD") or units.get("USD/shares") or units.get("shares") or units.get("pure") or []
        rows = []
        for fact in candidates:
            form = fact.get("form")
            if form not in {"10-Q", "10-K", "20-F", "40-F"}:
                continue
            start, end = fact.get("start"), fact.get("end")
            duration = None
            if start and end:
                try: duration = (date.fromisoformat(end) - date.fromisoformat(start)).days
                except ValueError: pass
            fp = fact.get("fp")
            is_quarter = duration is None or 60 <= duration <= 120 or fp in {"Q1", "Q2", "Q3"}
            if not is_quarter:
                continue
            rows.append({"period": end, "value": fact.get("val"), "fiscal_year": fact.get("fy"), "fiscal_period": fp, "filed": fact.get("filed"), "form": form, "accession": fact.get("accn"), "concept": tag, "unit": node.get("units") and next(iter(node["units"])), "duration_days": duration})
        if rows:
            dedup = {}
            for row in rows:
                key = (row["period"], row["fiscal_period"])
                current = dedup.get(key)
                row_rank = ((row.get("filed") or ""), -abs((row.get("duration_days") or 91) - 91))
                current_rank = ((current.get("filed") or ""), -abs((current.get("duration_days") or 91) - 91)) if current else None
                if current is None or row_rank > current_rank:
                    dedup[key] = row
            return sorted(dedup.values(), key=lambda x: x["period"] or "", reverse=True)[:16]
    return []


def fundamentals(ticker: str) -> dict:
    cik = cik_for_ticker(ticker)
    if not cik:
        return {"ticker": ticker, "metrics": {}}
    payload = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
    facts = payload.get("facts", {})
    metrics = {name: _quarterly_facts(facts, tags) for name, tags in CONCEPTS.items()}
    return {"ticker": ticker.upper(), "cik": cik, "entity_name": payload.get("entityName"), "metrics": metrics, "source": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"}


def filing_text(url: str) -> str:
    html = _get(url, json_response=False)
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def qualitative_signals(text: str) -> dict:
    lower = text.lower()
    themes = {
        "pricing": ["pricing", "price realization", "price/mix"],
        "volume": ["volume", "shipments", "demand"],
        "margins": ["margin", "productivity", "cost savings"],
        "inventory": ["inventory", "destocking", "restocking"],
        "guidance": ["guidance", "outlook", "forecast"],
        "capital": ["capital expenditure", "capex", "free cash flow"],
    }
    positive = ["strong", "improved", "growth", "record", "accelerat", "raised guidance", "above expectations"]
    negative = ["weak", "decline", "pressure", "headwind", "soft", "destocking", "lowered guidance", "below expectations"]
    return {
        "word_count": len(text.split()),
        "themes": {theme: sum(lower.count(term) for term in terms) for theme, terms in themes.items()},
        "positive_mentions": sum(lower.count(term) for term in positive),
        "negative_mentions": sum(lower.count(term) for term in negative),
    }
