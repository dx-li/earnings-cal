import json
import tempfile
from pathlib import Path
from unittest import TestCase

from earnings_cal.audit import AuditJournal
from earnings_cal import app as app_module


class AuditJournalTests(TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.journal = AuditJournal(Path(self.tempdir.name) / "audit.jsonl")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_revisions_are_append_only_and_hash_chained(self):
        event_id = self.journal.event_id("SHW", "2026-07-28T08:00:00-04:00", "FY2026 Q2")
        first = self.journal.append(
            event_id=event_id, ticker="SHW",
            event_date="2026-07-28T08:00:00-04:00", data={"call": "beat"},
        )
        second = self.journal.append(
            event_id=event_id, ticker="SHW",
            event_date="2026-07-29T08:00:00-04:00", data={"call": "miss"},
        )

        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["prev_hash"], first["hash"])
        self.assertTrue(self.journal.verify()["valid"])
        self.assertEqual(len(self.journal.for_event(event_id)), 2)

    def test_tampering_is_detected_and_blocks_append(self):
        event_id = self.journal.event_id("AVY", "2026-07-30T08:00:00-04:00", "FY2026 Q2")
        self.journal.append(
            event_id=event_id, ticker="AVY",
            event_date="2026-07-30T08:00:00-04:00", data={"call": "beat"},
        )
        record = self.journal.read_all()[0]
        record["data"]["call"] = "miss"
        self.journal.path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        self.assertFalse(self.journal.verify()["valid"])
        with self.assertRaises(ValueError):
            self.journal.append(
                event_id=event_id, ticker="AVY",
                event_date="2026-07-30T08:00:00-04:00", data={"call": "miss"},
            )

    def test_forecast_api_captures_consensus_and_revision(self):
        original = app_module.forecast_journal
        app_module.forecast_journal = self.journal
        try:
            response = app_module.app.test_client().post("/api/forecasts", json={
                "ticker": "SHW",
                "event_date": "2026-07-28T08:00:00-04:00",
                "call": "beat",
                "confidence": 70,
                "paper_side": "long",
                "paper_size": 1000,
                "consensus_snapshot": {"eps_estimate": 3.52},
            })
        finally:
            app_module.forecast_journal = original

        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertTrue(payload["integrity"]["valid"])
        self.assertEqual(payload["record"]["revision"], 1)
        self.assertEqual(payload["record"]["data"]["consensus_snapshot"]["eps_estimate"], 3.52)
