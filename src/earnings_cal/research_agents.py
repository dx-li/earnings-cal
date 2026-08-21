"""Agentic, review-by-exception research workflow for the isolated Lab."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import requests

from earnings_cal.research_sources import filing_text, filings, qualitative_signals
from earnings_cal.research_store import ResearchStore

CALL_TAXONOMY = {
    "demand": ["demand", "orders", "backlog", "end market"],
    "pricing": ["pricing", "price realization", "price/mix"],
    "volume": ["volume", "shipments", "utilization"],
    "costs": ["raw material", "labor", "freight", "energy", "inflation"],
    "margin": ["margin", "productivity", "cost reduction"],
    "inventory": ["inventory", "destocking", "restocking", "channel"],
    "guidance": ["guidance", "outlook", "forecast", "expect"],
    "capital_allocation": ["capex", "capital expenditure", "buyback", "dividend", "debt"],
    "portfolio": ["acquisition", "divestiture", "spin-off", "restructuring", "portfolio"],
}

EXTRACTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "kpis": {"type":"array","items":{"type":"object","additionalProperties":False,"properties":{
            "name":{"type":"string"},"unit":{"type":"string"},"segment":{"type":"string"},"period":{"type":"string"},"value":{"type":"string"},"definition":{"type":"string"},"source_quote":{"type":"string"},"confidence":{"type":"number"}},"required":["name","unit","segment","period","value","definition","source_quote","confidence"]}},
        "segments": {"type":"array","items":{"type":"object","additionalProperties":False,"properties":{
            "name":{"type":"string"},"effective_period":{"type":"string"},"status":{"type":"string"},"predecessors":{"type":"array","items":{"type":"string"}},"change_type":{"type":"string"},"source_quote":{"type":"string"},"confidence":{"type":"number"}},"required":["name","effective_period","status","predecessors","change_type","source_quote","confidence"]}},
        "themes": {"type":"array","items":{"type":"object","additionalProperties":False,"properties":{
            "category":{"type":"string"},"direction":{"type":"string"},"summary":{"type":"string"},"source_quote":{"type":"string"},"confidence":{"type":"number"}},"required":["category","direction","summary","source_quote","confidence"]}}
    }, "required":["kpis","segments","themes"]
}


def standardized_call(text: str) -> dict:
    lower = text.lower()
    categories = {}
    for category, terms in CALL_TAXONOMY.items():
        snippets = []
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if any(term in sentence.lower() for term in terms):
                snippets.append(sentence.strip()[:500])
        categories[category] = {"mentions": sum(lower.count(term) for term in terms), "evidence": snippets[:3]}
    return {"taxonomy_version":"1.0", "categories":categories, "signals":qualitative_signals(text)}


SYSTEM_PROMPT = "You extract auditable public-company research. Never infer a numeric value absent from the document. Preserve exact quotes. A renamed/reorganized segment is a new identity unless the document explicitly provides comparable recasts."


def _openai_extract(text: str, ticker: str, source_url: str, settings: dict, secrets: dict) -> tuple[dict | None, dict]:
    key = secrets.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")
    if not key:
        return None, {}
    payload = {
        "model": settings.get("model", "gpt-5-mini"),
        "input": [{"role":"system","content":[{"type":"input_text","text":SYSTEM_PROMPT}]},{"role":"user","content":[{"type":"input_text","text":f"Ticker: {ticker}\nSource: {source_url}\nExtract company-specific KPIs, segment lineage changes, and canonical earnings themes from this document:\n\n{text[:120000]}"}]}],
        "text": {"format":{"type":"json_schema","name":"earnings_research_extraction","strict":True,"schema":EXTRACTION_SCHEMA}},
    }
    response = requests.post("https://api.openai.com/v1/responses", headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"}, json=payload, timeout=180)
    response.raise_for_status()
    result = response.json()
    output_text = result.get("output_text")
    if not output_text:
        for item in result.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text": output_text = content.get("text")
    return (json.loads(output_text) if output_text else None), result.get("usage", {})


def _deepseek_extract(text: str, ticker: str, source_url: str, settings: dict, secrets: dict) -> tuple[dict | None, dict]:
    key = secrets.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None, {}
    prompt = f"Ticker: {ticker}\nSource: {source_url}\nExtract company-specific KPIs, segment lineage changes, and canonical earnings themes. Return one JSON object matching this schema exactly:\n{json.dumps(EXTRACTION_SCHEMA)}\n\nDocument:\n{text[:120000]}"
    payload = {"model":settings.get("model","deepseek-v4-flash"),"messages":[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":prompt}],"response_format":{"type":"json_object"}}
    response = requests.post("https://api.deepseek.com/chat/completions", headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"}, json=payload, timeout=180)
    response.raise_for_status(); result=response.json()
    content=((result.get("choices") or [{}])[0].get("message") or {}).get("content"); usage=result.get("usage",{})
    return (json.loads(content) if content else None), {"input_tokens":usage.get("prompt_tokens",0),"output_tokens":usage.get("completion_tokens",0)}


def _agent_extract(text: str, ticker: str, source_url: str, settings: dict, secrets: dict) -> tuple[dict | None, dict]:
    return (_deepseek_extract if settings.get("provider","openai")=="deepseek" else _openai_extract)(text,ticker,source_url,settings,secrets)


def _agent_enabled(settings: dict, secrets: dict) -> bool:
    if settings.get("provider","openai") == "deepseek": return bool(secrets.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY"))
    return bool(secrets.get("openai_api_key") or os.environ.get("OPENAI_API_KEY"))


class ResearchPipeline:
    def __init__(self, store: ResearchStore, config=None): self.store=store; self.config=config

    def run(self, ticker: str, *, historical=False) -> dict:
        ticker=ticker.upper(); data=self.store.normalize(ticker); started=datetime.now(timezone.utc).isoformat()
        settings=self.config.load() if self.config else {"start_year":2000,"documents_per_run":25,"model":"gpt-5-mini"}; secrets=self.config.secrets() if self.config else {}
        queue=[]; docs=[]; extracted=[]; total_input=0; total_output=0; processed={d.get("accession") for d in data["documents"]}
        candidates=filings(ticker, limit=5000 if historical else 12, start_year=int(settings["start_year"]) if historical else None)
        candidates=[f for f in candidates if f["accession"] not in processed]
        for filing in candidates:
            if filing["form"] not in {"8-K","10-Q","10-K"}: continue
            try:
                text=filing_text(filing["url"])
            except Exception as exc:
                queue.append(self._review("document_error", filing["filed"], {"message":str(exc),"source_url":filing["url"]}, 0)); continue
            projected_input=total_input+len(text[:120000])//4; projected_output=total_output+2500
            if self._cost(projected_input,projected_output,settings.get("model")) > float(settings.get("max_cost_usd",10)):
                queue.append(self._review("budget_limit",filing["filed"],{"message":f"Run stopped before {filing['accession']} at the configured cost ceiling.","source_url":filing["url"]},1)); break
            doc={"accession":filing["accession"],"form":filing["form"],"filed":filing["filed"],"source_url":filing["url"],"signals":qualitative_signals(text)}; docs.append(doc)
            agent,usage=_agent_extract(text,ticker,filing["url"],settings,secrets); total_input+=usage.get("input_tokens",0); total_output+=usage.get("output_tokens",0)
            if agent:
                extracted.append(agent)
                for kpi in agent["kpis"]:
                    queue.append(self._review("kpi_observation",kpi.get("period"),{**kpi,"source_url":filing["url"]},kpi["confidence"]))
                for segment in agent["segments"]:
                    queue.append(self._review("segment_lineage",segment.get("effective_period"),{**segment,"source_url":filing["url"]},segment["confidence"]))
            else:
                queue.append(self._review("agent_unavailable",filing["filed"],{"message":f"Add a {settings.get('provider','openai').title()} API key in Configuration to enable structured KPI and segment extraction.","source_url":filing["url"]},0))
            if len(docs)>=int(settings.get("documents_per_run",25) if historical else 3): break
        analyses=[]
        known={(row.get("transcript_id")) for row in data["call_analyses"]}
        for transcript in data["transcripts"]:
            if transcript["id"] in known: continue
            analyses.append({"transcript_id":transcript["id"],"period":transcript["period"],**standardized_call(transcript["text"])})
        data["documents"] = docs + [d for d in data["documents"] if d.get("accession") not in {x["accession"] for x in docs}]
        data["review_queue"].extend(queue); data["call_analyses"].extend(analyses)
        remaining=max(0,len(candidates)-len(docs)); run={"id":f"run-{len(data['pipeline_runs'])+1}","mode":"historical" if historical else "recent","started_at":started,"completed_at":datetime.now(timezone.utc).isoformat(),"documents":len(docs),"remaining_documents":remaining,"review_items":len(queue),"transcripts_standardized":len(analyses),"agent_enabled":_agent_enabled(settings,secrets),"provider":settings.get("provider","openai"),"model":settings.get("model"),"usage":{"input_tokens":total_input,"output_tokens":total_output},"estimated_cost_usd":self._cost(total_input,total_output,settings.get("model"))}
        data["pipeline_runs"].append(run); self.store.save(ticker,data); return run

    @staticmethod
    def _cost(input_tokens, output_tokens, model):
        rates={"gpt-5-mini":(0.25,2.0),"gpt-5.4-mini":(0.75,4.5),"deepseek-v4-flash":(0.14,0.28),"deepseek-v4-pro":(0.435,0.87)}
        inp,out=rates.get(model,rates["gpt-5-mini"])
        return round(input_tokens/1_000_000*inp+output_tokens/1_000_000*out,6)

    @staticmethod
    def _review(kind, period, proposal, confidence):
        return {"id":f"review-{datetime.now(timezone.utc).timestamp()}-{abs(hash(json.dumps(proposal,sort_keys=True)))%100000}","kind":kind,"period":period,"proposal":proposal,"confidence":confidence,"status":"pending","created_at":datetime.now(timezone.utc).isoformat()}

    def decide(self,ticker,item_id,decision):
        data=self.store.normalize(ticker); item=next((x for x in data["review_queue"] if x["id"]==item_id),None)
        if not item: raise KeyError(item_id)
        item["status"]=decision; item["decided_at"]=datetime.now(timezone.utc).isoformat()
        if decision=="accepted" and item["kind"]=="segment_lineage":
            p=item["proposal"]; seg_id=f"seg-{len(data['segments'])+1}"; data["segments"].append({"id":seg_id,**p})
            for predecessor in p.get("predecessors",[]): data["segment_edges"].append({"from_name":predecessor,"to_id":seg_id,"change_type":p.get("change_type"),"effective_period":p.get("effective_period"),"source_url":p.get("source_url")})
        if decision=="accepted" and item["kind"]=="kpi_observation":
            p=item["proposal"]; existing=next((x for x in data["kpi_schema"] if x["name"].lower()==p["name"].lower() and (x.get("segment")or"")==p.get("segment","")),None)
            if not existing:
                existing={"id":f"kpi-{len(data['kpi_schema'])+1}","name":p["name"],"unit":p["unit"],"segment":p["segment"],"definition":p["definition"],"aliases":""}; data["kpi_schema"].append(existing)
            data["observations"].append({"id":f"obs-{len(data['observations'])+1}","kpi_id":existing["id"],"period":p["period"],"value":p["value"],"unit":p["unit"],"source_url":p["source_url"],"source_quote":p["source_quote"],"confidence":p["confidence"],"recorded_at":datetime.now(timezone.utc).isoformat()})
        self.store.save(ticker,data); return item
