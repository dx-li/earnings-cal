"""Append-only, hash-chained forecast journal storage."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


GENESIS_HASH = "0" * 64


class AuditJournal:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    @staticmethod
    def event_id(ticker: str, event_date: str, fiscal_period: str | None = None) -> str:
        """Use the reported fiscal period; retain a legacy fallback for old clients."""
        if fiscal_period:
            compact = fiscal_period.upper().replace(" ", "")
            if compact.startswith("FY") and ":Q" not in compact:
                compact = compact.replace("Q", ":Q")
            return f"{ticker.strip().upper()}:{compact}"
        dt = datetime.fromisoformat(event_date.replace("Z", "+00:00"))
        quarter = (dt.month - 1) // 3 + 1
        return f"{ticker.strip().upper()}:{dt.year}:Q{quarter}"

    @staticmethod
    def _digest(record: dict) -> str:
        payload = {k: v for k, v in record.items() if k != "hash"}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def read_all(self) -> list[dict]:
        with self._lock:
            if not self.path.exists():
                return []
            records = []
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
            return records

    def verify(self) -> dict:
        previous = GENESIS_HASH
        records = self.read_all()
        for index, record in enumerate(records):
            if record.get("prev_hash") != previous or record.get("hash") != self._digest(record):
                return {"valid": False, "records": len(records), "broken_at": index}
            previous = record["hash"]
        return {"valid": True, "records": len(records), "head_hash": previous}

    def append(self, *, event_id: str, ticker: str, event_date: str, data: dict,
               supersedes_event_id: str | None = None,
               original_recorded_at: str | None = None) -> dict:
        with self._lock:
            verification = self.verify()
            if not verification["valid"]:
                raise ValueError("Audit journal integrity check failed; refusing to append")
            records = self.read_all()
            revision = 1 + sum(r.get("event_id") == event_id for r in records)
            record = {
                "record_id": str(uuid.uuid4()),
                "event_id": event_id,
                "revision": revision,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "ticker": ticker.strip().upper(),
                "event_date_at_entry": event_date,
                "data": data,
                "prev_hash": records[-1]["hash"] if records else GENESIS_HASH,
            }
            if supersedes_event_id:
                record["supersedes_event_id"] = supersedes_event_id
            if original_recorded_at:
                record["original_recorded_at"] = original_recorded_at
            record["hash"] = self._digest(record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as out:
                out.write(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
                out.flush()
            return record

    def for_event(self, event_id: str) -> list[dict]:
        return [r for r in self.read_all() if r.get("event_id") == event_id]

    def latest(self) -> list[dict]:
        latest_by_event: dict[str, dict] = {}
        superseded = set()
        for record in self.read_all():
            latest_by_event[record["event_id"]] = record
            if record.get("supersedes_event_id"):
                superseded.add(record["supersedes_event_id"])
        return sorted(
            (r for key, r in latest_by_event.items() if key not in superseded),
            key=lambda r: r.get("original_recorded_at") or r["recorded_at"], reverse=True,
        )
