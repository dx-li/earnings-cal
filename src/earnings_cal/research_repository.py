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
                CREATE TABLE IF NOT EXISTS earnings_brief_history (
                    ticker TEXT NOT NULL, release_date TEXT NOT NULL, fiscal_period TEXT NOT NULL,
                    generated_at TEXT NOT NULL, model TEXT, lake_path TEXT NOT NULL,
                    brief_json TEXT NOT NULL, run_id TEXT,
                    PRIMARY KEY(ticker, release_date, fiscal_period),
                    FOREIGN KEY(run_id) REFERENCES research_runs(id)
                );
                CREATE TABLE IF NOT EXISTS brief_jobs (
                    ticker TEXT NOT NULL, release_date TEXT NOT NULL, fiscal_period TEXT NOT NULL,
                    company_name TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    queued_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, error TEXT,
                    PRIMARY KEY(ticker, release_date, fiscal_period)
                );
                CREATE TABLE IF NOT EXISTS data_assets (
                    name TEXT PRIMARY KEY, category TEXT NOT NULL, format TEXT NOT NULL,
                    lake_path TEXT NOT NULL, legacy_path TEXT, migrated_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_ticker ON source_artifacts(ticker, captured_at);
                CREATE INDEX IF NOT EXISTS idx_runs_ticker ON research_runs(ticker, started_at);
            """)

    def operational_path(self, name: str, filename: str, legacy_path: Path | None = None) -> Path:
        """Return a lake path, copying a legacy file once without deleting it."""
        folder = self.lake / "operational"
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / filename
        migrated_at = None
        if not destination.exists() and legacy_path and legacy_path.is_file():
            shutil.copy2(legacy_path, destination)
            migrated_at = self._now()
        file_format = destination.suffix.lstrip(".") or "binary"
        with self.connect() as db:
            db.execute("""INSERT INTO data_assets(name,category,format,lake_path,legacy_path,migrated_at,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET lake_path=excluded.lake_path,
                legacy_path=excluded.legacy_path,updated_at=excluded.updated_at,
                migrated_at=COALESCE(data_assets.migrated_at,excluded.migrated_at)""",
                (name, "operational", file_format, destination.relative_to(self.root).as_posix(),
                 str(legacy_path) if legacy_path else None, migrated_at, self._now()))
        return destination

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
        release_date = str(brief.get("release_date") or "unknown")[:10]
        fiscal_period = str(brief.get("fiscal_period") or brief.get("period") or "unknown")
        safe_period = "".join(c if c.isalnum() else "-" for c in fiscal_period).strip("-")
        path = self.curated / ticker.upper() / f"{release_date}-{safe_period}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        payload = json.dumps(brief, indent=2, ensure_ascii=False)
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
        with self.connect() as db:
            db.execute("""INSERT INTO earnings_brief_history(ticker,release_date,fiscal_period,generated_at,model,lake_path,brief_json,run_id)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(ticker,release_date,fiscal_period) DO UPDATE SET
                generated_at=excluded.generated_at,model=excluded.model,lake_path=excluded.lake_path,
                brief_json=excluded.brief_json,run_id=excluded.run_id""",
                (ticker.upper(), release_date, fiscal_period, brief.get("generated_at") or self._now(), brief.get("model"),
                 path.relative_to(self.root).as_posix(), payload, run_id))
            db.execute("""INSERT INTO earnings_briefs(ticker,period,generated_at,model,lake_path,brief_json,run_id)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET period=excluded.period,
                generated_at=excluded.generated_at,model=excluded.model,lake_path=excluded.lake_path,
                brief_json=excluded.brief_json,run_id=excluded.run_id""",
                (ticker.upper(), brief.get("fiscal_period") or brief.get("period"), brief.get("generated_at") or self._now(), brief.get("model"),
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

    def brief_for_event(self, ticker: str, release_date: str, fiscal_period: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("""SELECT brief_json FROM earnings_brief_history
                WHERE ticker=? AND release_date=? AND fiscal_period=?""",
                (ticker.upper(), release_date[:10], fiscal_period)).fetchone()
        return json.loads(row["brief_json"]) if row else None

    def list_briefs(self, ticker: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""SELECT release_date,fiscal_period,generated_at,brief_json
                FROM earnings_brief_history WHERE ticker=? ORDER BY release_date DESC""", (ticker.upper(),)).fetchall()
        return [{"release_date":row["release_date"],"fiscal_period":row["fiscal_period"],
                 "generated_at":row["generated_at"],"brief":json.loads(row["brief_json"])} for row in rows]

    def enqueue_brief(self, ticker: str, release_date: str, fiscal_period: str, company_name: str | None) -> bool:
        if self.brief_for_event(ticker, release_date, fiscal_period):
            return False
        with self.connect() as db:
            cursor = db.execute("""INSERT INTO brief_jobs(ticker,release_date,fiscal_period,company_name,status,queued_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(ticker,release_date,fiscal_period) DO UPDATE SET
                status=CASE WHEN brief_jobs.status='complete' THEN 'complete' ELSE 'queued' END,
                company_name=excluded.company_name,queued_at=excluded.queued_at,error=NULL""",
                (ticker.upper(), release_date[:10], fiscal_period, company_name, "queued", self._now()))
        return cursor.rowcount > 0

    def next_brief_job(self) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM brief_jobs WHERE status='queued' ORDER BY queued_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE brief_jobs SET status='running',started_at=?,attempts=attempts+1 WHERE ticker=? AND release_date=? AND fiscal_period=?",
                       (self._now(), row["ticker"], row["release_date"], row["fiscal_period"]))
        return dict(row)

    def finish_brief_job(self, job: dict, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("""UPDATE brief_jobs SET status=?,completed_at=?,error=?
                WHERE ticker=? AND release_date=? AND fiscal_period=?""",
                ("failed" if error else "complete", self._now(), error,
                 job["ticker"], job["release_date"], job["fiscal_period"]))

    def brief_job_counts(self) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT status,COUNT(*) AS n FROM brief_jobs GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    def brief_job_statuses(self) -> dict:
        with self.connect() as db:
            rows = db.execute("SELECT ticker,status,release_date,fiscal_period,error FROM brief_jobs").fetchall()
        return {row["ticker"]: dict(row) for row in rows}

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
