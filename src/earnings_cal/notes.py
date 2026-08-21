"""Append-only, hash-chained earnings notes."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

GENESIS_HASH = "0" * 64


class NotesJournal:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    @staticmethod
    def _digest(record: dict) -> str:
        payload = {k: v for k, v in record.items() if k != "hash"}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def read_all(self) -> list[dict]:
        with self._lock:
            if not self.path.exists():
                return []
            return [
                json.loads(line)
                for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

    def verify(self) -> dict:
        previous = GENESIS_HASH
        records = self.read_all()
        for index, record in enumerate(records):
            if record.get("prev_hash") != previous or record.get("hash") != self._digest(record):
                return {"valid": False, "records": len(records), "broken_at": index}
            previous = record["hash"]
        return {"valid": True, "records": len(records), "head_hash": previous}

    def append(self, data: dict, note_id: str | None = None) -> dict:
        with self._lock:
            integrity = self.verify()
            if not integrity["valid"]:
                raise ValueError("Notes audit integrity check failed; refusing to append")
            records = self.read_all()
            note_id = note_id or str(uuid.uuid4())
            prior = [r for r in records if r.get("note_id") == note_id]
            if note_id and not prior and any(r.get("note_id") == note_id for r in records):
                raise ValueError("Invalid note revision")
            record = {
                "note_id": note_id,
                "revision": len(prior) + 1,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "data": data,
                "prev_hash": records[-1]["hash"] if records else GENESIS_HASH,
            }
            record["hash"] = self._digest(record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as out:
                out.write(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
                out.flush()
            return record

    def latest(self) -> list[dict]:
        notes: dict[str, dict] = {}
        for record in self.read_all():
            notes[record["note_id"]] = record
        return sorted(notes.values(), key=lambda r: r["recorded_at"], reverse=True)

    def history(self, note_id: str) -> list[dict]:
        return [r for r in self.read_all() if r.get("note_id") == note_id]
