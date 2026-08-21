"""Local research lake and SQLite catalog.

The lake keeps inspectable JSON artifacts; SQLite provides fast cache and lineage
queries. Nothing in this module writes to Windows AppData except the optional,
read-only legacy import.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class ResearchRepository:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.lake = self.root / "lake"
        self.bronze = self.lake / "bronze"
        self.curated = self.lake / "curated" / "earnings-briefs"
        self.db_path = self.root / "earnings-research.sqlite3"
        self.bronze.mkdir(parents=True, exist_ok=True)
        self.curated.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._import_legacy_briefs()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS research_runs (
                    id TEXT PRIMARY KEY, ticker TEXT NOT NULL, model TEXT NOT NULL,
                    status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0, error TEXT
                );
                CREATE TABLE IF NOT EXISTS source_artifacts (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ticker TEXT NOT NULL,
                    tool TEXT NOT NULL, step INTEGER NOT NULL, ok INTEGER NOT NULL,
                    lake_path TEXT NOT NULL, sha256 TEXT NOT NULL, captured_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES research_runs(id)
                );
                CREATE TABLE IF NOT EXISTS earnings_briefs (
                    ticker TEXT PRIMARY KEY, period TEXT, generated_at TEXT NOT NULL,
                    model TEXT, lake_path TEXT NOT NULL, brief_json TEXT NOT NULL,
                    run_id TEXT, FOREIGN KEY(run_id) REFERENCES research_runs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_ticker ON source_artifacts(ticker, captured_at);
                CREATE INDEX IF NOT EXISTS idx_runs_ticker ON research_runs(ticker, started_at);
            """)

    def begin_run(self, ticker: str, model: str) -> str:
        run_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO research_runs(id,ticker,model,status,started_at) VALUES(?,?,?,?,?)",
                       (run_id, ticker.upper(), model, "running", self._now()))
        return run_id

    def record_tool(self, run_id: str, ticker: str, step: int, tool: str, ok: bool, payload: dict) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        folder = self.bronze / ticker.upper() / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{run_id}-{step:02d}-{tool}-{digest[:10]}.json"
        path.write_bytes(raw)
        artifact_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO source_artifacts VALUES(?,?,?,?,?,?,?,?,?)",
                       (artifact_id, run_id, ticker.upper(), tool, step, int(ok),
                        path.relative_to(self.root).as_posix(), digest, self._now()))
        return artifact_id

    def finish_run(self, run_id: str, usage: dict, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE research_runs SET status=?,completed_at=?,input_tokens=?,output_tokens=?,error=? WHERE id=?",
                       ("failed" if error else "complete", self._now(), usage.get("input_tokens", 0),
                        usage.get("output_tokens", 0), error, run_id))

    def save_brief(self, ticker: str, brief: dict, run_id: str | None) -> Path:
        path = self.curated / f"{ticker.upper()}-latest.json"
        tmp = path.with_suffix(".tmp")
        payload = json.dumps(brief, indent=2, ensure_ascii=False)
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
        with self.connect() as db:
            db.execute("""INSERT INTO earnings_briefs(ticker,period,generated_at,model,lake_path,brief_json,run_id)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET period=excluded.period,
                generated_at=excluded.generated_at,model=excluded.model,lake_path=excluded.lake_path,
                brief_json=excluded.brief_json,run_id=excluded.run_id""",
                (ticker.upper(), brief.get("period"), brief.get("generated_at") or self._now(), brief.get("model"),
                 path.relative_to(self.root).as_posix(), payload, run_id))
        return path

    def cached_brief(self, ticker: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT brief_json FROM earnings_briefs WHERE ticker=?", (ticker.upper(),)).fetchone()
        if row:
            return json.loads(row["brief_json"])
        path = self.curated / f"{ticker.upper()}-latest.json"
        try:
            brief = json.loads(path.read_text(encoding="utf-8"))
            self.save_brief(ticker, brief, brief.get("run_id"))
            return brief
        except Exception:
            return None

    def _import_legacy_briefs(self) -> None:
        base = Path(os.environ.get("APPDATA") or Path.home()) / "earnings-cal" / "research-briefs"
        if not base.is_dir():
            return
        for old in base.glob("*-latest-brief.json"):
            ticker = old.name.removesuffix("-latest-brief.json").upper()
            if self.cached_brief(ticker):
                continue
            try:
                brief = json.loads(old.read_text(encoding="utf-8"))
                self.save_brief(ticker, brief, None)
                archive = self.bronze / "legacy-import"
                archive.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old, archive / old.name)
            except Exception:
                continue

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
