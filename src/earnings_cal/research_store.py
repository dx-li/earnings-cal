"""Persistent storage for the isolated Earnings Research Lab."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class ResearchStore:
    def __init__(self, root: Path | None = None):
        base = Path(os.environ.get("APPDATA") or Path.home())
        self.root = root or base / "earnings-research-lab"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, ticker: str) -> Path:
        return self.root / f"{ticker.upper()}.json"

    def load(self, ticker: str) -> dict:
        try:
            return json.loads(self._path(ticker).read_text(encoding="utf-8"))
        except Exception:
            return {"ticker": ticker.upper(), "kpi_schema": [], "observations": [], "transcripts": [], "documents": [], "segments": [], "segment_edges": [], "review_queue": [], "pipeline_runs": [], "call_analyses": []}

    def save(self, ticker: str, data: dict) -> None:
        path = self._path(ticker)
        data["ticker"] = ticker.upper()
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)

    def normalize(self, ticker: str) -> dict:
        data = self.load(ticker)
        for key in ("kpi_schema", "observations", "transcripts", "documents", "segments", "segment_edges", "review_queue", "pipeline_runs", "call_analyses"):
            data.setdefault(key, [])
        return data

    def add_kpi(self, ticker: str, kpi: dict) -> dict:
        data = self.load(ticker)
        kpi = {**kpi, "id": kpi.get("id") or f"kpi-{len(data['kpi_schema']) + 1}"}
        data["kpi_schema"] = [row for row in data["kpi_schema"] if row.get("id") != kpi["id"]] + [kpi]
        self.save(ticker, data)
        return kpi

    def add_observation(self, ticker: str, observation: dict) -> dict:
        data = self.load(ticker)
        observation = {**observation, "id": f"obs-{len(data['observations']) + 1}", "recorded_at": datetime.now(timezone.utc).isoformat()}
        data["observations"].append(observation)
        self.save(ticker, data)
        return observation

    def add_transcript(self, ticker: str, transcript: dict) -> dict:
        data = self.load(ticker)
        transcript = {**transcript, "id": f"tx-{len(data['transcripts']) + 1}", "imported_at": datetime.now(timezone.utc).isoformat()}
        data["transcripts"].append(transcript)
        self.save(ticker, data)
        return transcript
