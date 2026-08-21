"""Standalone Earnings Research Lab; isolated from the production calendar."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from earnings_cal.research_sources import filings, filing_text, fundamentals, qualitative_signals
from earnings_cal.research_store import ResearchStore
from earnings_cal.research_agents import ResearchPipeline
from earnings_cal.research_config import ResearchConfig


def _bundle_root() -> Path:
    return Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent  # type: ignore[attr-defined]


ASSETS = _bundle_root() / "research_assets"
CALENDAR_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "earnings-cal"
store = ResearchStore()
config = ResearchConfig(store.root)
pipeline = ResearchPipeline(store, config)
app = Flask(__name__, static_folder=None)


def _read_json(path: Path, fallback):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return fallback


def calendar_context(ticker: str) -> dict:
    snapshot = _read_json(CALENDAR_DIR / "snapshot.json", {}).get(ticker, {}).get("data", {})
    archive = list(_read_json(CALENDAR_DIR / "archive.json", {}).get(ticker, {}).values())
    return {"ticker": ticker, "name": snapshot.get("name"), "sector": snapshot.get("sector"), "industry": snapshot.get("industry"), "events": sorted(archive, key=lambda x: x.get("date", ""), reverse=True)}


@app.route("/")
def index(): return send_from_directory(ASSETS, "index.html")


@app.route("/api/tickers")
def tickers():
    return jsonify(_read_json(CALENDAR_DIR / "tickers.json", []))


@app.route("/api/company/<ticker>")
def company(ticker: str):
    ticker = ticker.upper()
    return jsonify({"calendar": calendar_context(ticker), "research": store.normalize(ticker)})


@app.route("/api/sec/<ticker>/fundamentals")
def sec_fundamentals(ticker: str): return jsonify(fundamentals(ticker.upper()))


@app.route("/api/sec/<ticker>/filings")
def sec_filings(ticker: str): return jsonify(filings(ticker.upper()))


@app.route("/api/sec/document", methods=["POST"])
def sec_document():
    body = request.get_json() or {}
    url = str(body.get("url") or "")
    if not url.startswith("https://www.sec.gov/Archives/"):
        return jsonify({"error": "Only SEC archive URLs are supported"}), 400
    text = filing_text(url)
    return jsonify({"text": text[:250000], "signals": qualitative_signals(text), "source_url": url})


@app.route("/api/research/<ticker>/kpis", methods=["POST"])
def add_kpi(ticker: str):
    body = request.get_json() or {}
    if not str(body.get("name") or "").strip(): return jsonify({"error": "name is required"}), 400
    return jsonify(store.add_kpi(ticker, {k: body.get(k) for k in ("id", "name", "unit", "definition", "aliases", "segment")})), 201


@app.route("/api/research/<ticker>/observations", methods=["POST"])
def add_observation(ticker: str):
    body = request.get_json() or {}
    if not body.get("kpi_id") or not body.get("period"): return jsonify({"error": "kpi_id and period are required"}), 400
    return jsonify(store.add_observation(ticker, {k: body.get(k) for k in ("kpi_id", "period", "value", "unit", "source_url", "source_quote", "definition_note")})), 201


@app.route("/api/research/<ticker>/transcripts", methods=["POST"])
def add_transcript(ticker: str):
    body = request.get_json() or {}
    text = str(body.get("text") or "").strip()
    if not text or not body.get("period"): return jsonify({"error": "period and text are required"}), 400
    row = store.add_transcript(ticker, {"period": body.get("period"), "source_url": body.get("source_url"), "text": text, "signals": qualitative_signals(text)})
    return jsonify(row), 201


@app.route("/api/research/<ticker>/pipeline", methods=["POST"])
def run_pipeline(ticker: str):
    try:
        return jsonify(pipeline.run(ticker, historical=(request.args.get("historical") == "1")))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/research/<ticker>/review/<item_id>", methods=["POST"])
def decide_review(ticker: str, item_id: str):
    decision = (request.get_json() or {}).get("decision")
    if decision not in {"accepted", "rejected"}:
        return jsonify({"error": "decision must be accepted or rejected"}), 400
    try:
        return jsonify(pipeline.decide(ticker, item_id, decision))
    except KeyError:
        return jsonify({"error": "review item not found"}), 404


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        try: return jsonify(config.save(request.get_json() or {}))
        except Exception as exc: return jsonify({"error":str(exc)}),400
    return jsonify(config.load())
