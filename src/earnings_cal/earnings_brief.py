"""Bounded agent harness for source-backed latest-earnings briefs."""
from __future__ import annotations

import json
import os
import re
import ipaddress
import socket
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
from edgar import Company, set_identity
from earnings_cal.research_repository import ResearchRepository

BRIEF_SHAPE = {"period":"fiscal period","verdict":"quality of print","top_line":{"summary":"","drivers":[]},"bottom_line":{"summary":"","drivers":[]},"cash_inventory":{"summary":"","drivers":[]},"watch_items":[],"sources":[{"title":"","url":"","type":"filing|transcript|web"}],"limitations":[]}
TOOLS = [
    {"type":"function","function":{"name":"get_recent_filings","description":"Get recent official SEC earnings filings through EdgarTools. Use this first.","parameters":{"type":"object","properties":{"forms":{"type":"array","items":{"type":"string"}},"limit":{"type":"integer"}},"required":["forms"]}}},
    {"type":"function","function":{"name":"search_earnings_transcript","description":"Search the public web for the latest earnings transcript or prepared remarks.","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}},
    {"type":"function","function":{"name":"fetch_web_page","description":"Fetch readable text from a transcript or company IR page returned by search.","parameters":{"type":"object","properties":{"url":{"type":"string"}},"required":["url"]}}},
]

def _plain_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|svg).*?>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", unescape(re.sub(r"(?s)<[^>]+>", " ", html))).strip()

class EarningsBriefHarness:
    """Small observable tool loop; the model cannot access arbitrary local state."""
    def __init__(self, repository: ResearchRepository, api_key: str, model: str = "deepseek-v4-flash"):
        self.repository, self.api_key, self.model = repository, api_key, model
        self.http = requests.Session()
        identity = os.getenv("EDGAR_IDENTITY", "earnings-cal research@example.com")
        self.http.headers.update({"User-Agent": identity})
        set_identity(identity)

    def cached(self, ticker: str) -> dict | None:
        return self.repository.cached_brief(ticker)

    def run(self, ticker: str, company_name: str | None = None) -> dict:
        ticker = ticker.upper()
        run_id = self.repository.begin_run(ticker, self.model)
        usage = {"input_tokens":0,"output_tokens":0}
        try:
            return self._run(ticker, company_name, run_id, usage)
        except Exception as exc:
            self.repository.finish_run(run_id, usage, str(exc))
            raise

    def _run(self, ticker: str, company_name: str | None, run_id: str, usage: dict) -> dict:
        messages = [
            {"role":"system","content":"You are a public-equity earnings analyst in a bounded research harness. Use official SEC filings first, then find the earnings transcript or prepared remarks. Never invent a number, quote, or cause. Separate reported facts from management explanations. If evidence is unavailable, say so. Return concise JSON only when research is complete."},
            {"role":"user","content":f"Research {company_name or ticker} ({ticker})'s most recent reported earnings. Explain top-line, bottom-line, cash-flow/working-capital/inventory drivers. Use tools, then return JSON matching this exact shape: {json.dumps(BRIEF_SHAPE)}"},
        ]
        trace = []
        for step in range(6):
            response = self.http.post("https://api.deepseek.com/chat/completions", headers={"Authorization":f"Bearer {self.api_key}"}, json={"model":self.model,"messages":messages,"tools":TOOLS,"tool_choice":"auto","thinking":{"type":"disabled"},"max_tokens":4000,"temperature":0.1}, timeout=180)
            response.raise_for_status(); body=response.json(); counts=body.get("usage") or {}
            usage["input_tokens"] += counts.get("prompt_tokens",0); usage["output_tokens"] += counts.get("completion_tokens",0)
            message=((body.get("choices") or [{}])[0].get("message") or {}); calls=message.get("tool_calls") or []
            if not calls:
                if not (message.get("content") or "").strip():
                    raise RuntimeError(f"DeepSeek returned no final content (finish_reason={((body.get('choices') or [{}])[0].get('finish_reason'))})")
                try:
                    result=self._parse_result(message.get("content") or "")
                except json.JSONDecodeError:
                    messages.append({"role":"assistant","content":message.get("content")})
                    messages.append({"role":"user","content":f"Return the completed answer now as one JSON object only, matching this exact shape: {json.dumps(BRIEF_SHAPE)}"})
                    continue
                result.update({"ticker":ticker,"generated_at":datetime.now(timezone.utc).isoformat(),"model":self.model,"usage":usage,"trace":trace})
                result["run_id"] = run_id
                self._validate(result)
                self.repository.save_brief(ticker, result, run_id)
                self.repository.finish_run(run_id, usage)
                return result
            messages.append({k:v for k,v in message.items() if k in {"role","content","tool_calls","reasoning_content"}})
            for call in calls:
                name=call.get("function",{}).get("name","")
                try: output=self._tool(ticker,name,json.loads(call.get("function",{}).get("arguments") or "{}")); ok=True
                except Exception as exc: output={"error":str(exc)}; ok=False
                trace.append({"step":step+1,"tool":name,"ok":ok})
                self.repository.record_tool(run_id, ticker, step + 1, name, ok, output)
                messages.append({"role":"tool","tool_call_id":call.get("id"),"content":json.dumps(output)[:120000]})
        error = "Research stopped at the six-step safety limit before producing a brief"
        raise RuntimeError(error)

    def _tool(self, ticker: str, name: str, args: dict):
        if name == "get_recent_filings":
            forms=[f for f in args.get("forms",[]) if f in {"8-K","10-Q","10-K","6-K","20-F"}] or ["8-K","10-Q"]
            filings=Company(ticker).get_filings(form=forms).head(min(max(int(args.get("limit",2)),1),2)); rows=[]
            for filing in filings:
                rows.append({"form":filing.form,"filed":str(filing.filing_date),"accession":filing.accession_no,"url":filing.homepage_url,"text":filing.markdown()[:45000]})
            return {"source":"SEC EDGAR via EdgarTools","filings":rows}
        if name == "search_earnings_transcript":
            query=str(args.get("query") or f"{ticker} latest earnings call transcript")
            html=self.http.get(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",timeout=25).text; hits=[]
            for href,title in re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',html,re.I|re.S):
                parsed=urlparse(unescape(href)); target=unquote(parse_qs(parsed.query).get("uddg",[href])[0])
                if target.startswith("http"): hits.append({"title":_plain_text(title),"url":target})
            return {"query":query,"results":hits[:8]}
        if name == "fetch_web_page":
            url=str(args.get("url") or "")
            parsed=urlparse(url)
            if parsed.scheme not in {"http","https"} or not parsed.hostname: raise ValueError("Only public HTTP(S) URLs are allowed")
            for address in socket.getaddrinfo(parsed.hostname, None):
                if ipaddress.ip_address(address[4][0]).is_private or ipaddress.ip_address(address[4][0]).is_loopback:
                    raise ValueError("Private network URLs are not allowed")
            response=self.http.get(url,timeout=30); response.raise_for_status()
            return {"url":response.url,"text":_plain_text(response.text)[:40000]}
        raise ValueError(f"Unknown tool: {name}")

    @staticmethod
    def _parse_result(content: str) -> dict:
        content=content.strip()
        if content.startswith("```"): content=re.sub(r"^```(?:json)?\s*|\s*```$","",content,flags=re.I)
        if not content.startswith("{") and "{" in content and "}" in content:
            content=content[content.find("{"):content.rfind("}")+1]
        return json.loads(content)

    @staticmethod
    def _validate(result: dict) -> None:
        required={"period","verdict","top_line","bottom_line","cash_inventory","watch_items","sources","limitations"}; missing=required-result.keys()
        if missing: raise ValueError(f"Model response missing fields: {', '.join(sorted(missing))}")
        if not result["sources"]: raise ValueError("Model response contained no source citations")
