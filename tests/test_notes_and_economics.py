import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from earnings_cal import app as app_module
from earnings_cal.notes import NotesJournal


class NotesAndEconomicsTests(TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.journal = NotesJournal(Path(self.tempdir.name) / "notes.jsonl")
        app_module._economic_range_cache.clear()
        app_module._sec_revenue_cache.clear()
        app_module._sec_cik_by_ticker = None

    def tearDown(self):
        self.tempdir.cleanup()

    def test_note_revisions_are_append_only(self):
        first = self.journal.append({
            "ticker": "SXT", "calendar_quarter": "2026 Q2",
            "title": "Initial", "body": "Margins improved",
        })
        second = self.journal.append({
            "ticker": "SXT", "calendar_quarter": "2026 Q2",
            "title": "Updated", "body": "Added guidance notes",
        }, note_id=first["note_id"])

        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["prev_hash"], first["hash"])
        self.assertEqual(len(self.journal.latest()), 1)
        self.assertTrue(self.journal.verify()["valid"])

    def test_notes_api_assigns_calendar_quarter(self):
        original = app_module.notes_journal
        app_module.notes_journal = self.journal
        try:
            response = app_module.app.test_client().post("/api/notes", json={
                "ticker": "SXT",
                "event_date": "2026-07-24T08:00:00-04:00",
                "fiscal_period": "FY2026 Q2",
                "title": "Earnings takeaways",
                "body": "Pricing remained strong.",
                "tags": ["pricing"],
            })
        finally:
            app_module.notes_journal = original

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.get_json()["record"]["data"]["calendar_quarter"], "2026 Q3"
        )

    def test_bea_schedule_is_normalized(self):
        html = b"""
        <table><tr><th>Year 2026</th><th>Release</th></tr>
        <tr><td>August 26 8:30 AM</td><td>News GDP (Second Estimate), Q2 2026</td></tr>
        </table>
        """
        response = Mock(content=html)
        response.raise_for_status.return_value = None
        app_module._economic_cache = None
        with patch.object(app_module.requests, "get", return_value=response):
            events = app_module._fetch_bea_releases()

        self.assertEqual(len(events), 1)
        self.assertIn("GDP", events[0]["title"])
        self.assertEqual(events[0]["date"], "2026-08-26T08:30:00-04:00")

    def test_bls_ics_is_normalized(self):
        ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:cpi-2026-08
DTSTART;TZID=US-Eastern:20260812T083000
SUMMARY:Consumer Price Index
CATEGORIES:IMPORTANT, BLS
END:VEVENT
END:VCALENDAR
"""
        response = Mock(text=ics)
        response.raise_for_status.return_value = None
        app_module._bls_cache = None
        with patch.object(app_module.browser_requests, "get", return_value=response) as get:
            events = app_module._fetch_bls_releases()

        get.assert_called_once_with(
            "https://www.bls.gov/schedule/news_release/bls.ics",
            impersonate="chrome",
            timeout=10,
        )
        self.assertEqual(events, [{
            "id": "BLS:cpi-2026-08",
            "date": "2026-08-12T08:30:00-04:00",
            "title": "Consumer Price Index",
            "source": "U.S. Bureau of Labor Statistics",
            "source_url": "https://www.bls.gov/schedule/",
            "source_link_type": "calendar",
            "category": "economic",
            "country": "United States",
            "period": "",
        }])

    def test_eurostat_calendar_is_normalized(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = [{
            "recordid": "123",
            "start": "2026-08-13T11:00Z",
            "title": "Industrial production",
            "period": "June 2026",
            "datasetCodes": "sts_inpr_m, sts_inpr_q",
        }]
        with patch.object(app_module.requests, "get", return_value=response):
            events = app_module._fetch_eurostat_releases(
                app_module.date(2026, 8, 1), app_module.date(2026, 8, 31)
            )

        self.assertEqual(events[0]["country"], "Euro area / European Union")
        self.assertEqual(events[0]["source_link_type"], "calendar")
        self.assertEqual(events[0]["dataset_codes"], ["sts_inpr_m", "sts_inpr_q"])
        self.assertEqual(events[0]["period"], "June 2026")

    def test_ons_calendar_is_normalized(self):
        response = Mock(content=b"""
        <a data-gtm-release-date="20260819" data-gtm-release-time="07:00"
           href="/releases/consumerpriceinflationukjuly2026">
          Consumer price inflation, UK: July 2026
        </a>
        """)
        response.raise_for_status.return_value = None
        with patch.object(app_module.requests, "get", return_value=response):
            events = app_module._fetch_ons_releases(
                app_module.date(2026, 8, 1), app_module.date(2026, 8, 31)
            )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["country"], "United Kingdom")
        self.assertEqual(events[0]["date"], "2026-08-19T07:00:00+01:00")

    def test_economic_api_caches_complete_date_range(self):
        event = {
            "id": "TEST:1", "date": "2026-08-12T08:30:00-04:00",
            "title": "Test release", "source": "Test", "source_url": "",
            "category": "economic", "country": "United States", "period": "",
        }
        fetchers = (
            "_fetch_bea_releases", "_fetch_bls_releases", "_fetch_eurostat_releases",
            "_fetch_ons_releases", "_fetch_ibge_releases", "_fetch_china_nbs_releases",
        )
        patches = [patch.object(app_module, name, return_value=[event] if i == 0 else [])
                   for i, name in enumerate(fetchers)]
        mocks = [item.start() for item in patches]
        try:
            client = app_module.app.test_client()
            first = client.get("/api/economic-releases?start=2026-08-01&end=2026-08-31")
            second = client.get("/api/economic-releases?start=2026-08-01&end=2026-08-31")
        finally:
            for item in patches:
                item.stop()

        self.assertFalse(first.get_json()["cached"])
        self.assertTrue(second.get_json()["cached"])
        self.assertEqual(second.get_json()["events"], [event])
        for mocked in mocks:
            mocked.assert_called_once()

    def test_economic_api_sorts_different_timezones_by_instant(self):
        us_event = {
            "id": "US:LATE", "date": "2026-08-01T18:00:00-04:00",
            "title": "Later instant", "source": "Test", "source_url": "",
            "category": "economic", "country": "United States", "period": "",
        }
        china_event = {
            "id": "CN:EARLY", "date": "2026-08-02T01:00:00+08:00",
            "title": "Earlier instant", "source": "Test", "source_url": "",
            "source_timezone": "Asia/Shanghai",
            "category": "economic", "country": "China", "period": "",
        }
        with patch.object(app_module, "_fetch_bea_releases", return_value=[us_event]), \
             patch.object(app_module, "_fetch_bls_releases", return_value=[]), \
             patch.object(app_module, "_fetch_eurostat_releases", return_value=[]), \
             patch.object(app_module, "_fetch_ons_releases", return_value=[]), \
             patch.object(app_module, "_fetch_ibge_releases", return_value=[]), \
             patch.object(app_module, "_fetch_china_nbs_releases", return_value=[china_event]):
            response = app_module.app.test_client().get(
                "/api/economic-releases?start=2026-08-01&end=2026-08-02"
            )

        self.assertEqual(
            [event["id"] for event in response.get_json()["events"]],
            ["CN:EARLY", "US:LATE"],
        )

    def test_sec_company_facts_selects_quarter_not_year_to_date(self):
        ticker_payload = {"0": {"ticker": "TEST", "cik_str": 123}}
        facts_payload = {"facts": {"us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                {"start": "2026-01-01", "end": "2026-06-30", "val": 1900,
                 "filed": "2026-07-31", "form": "10-Q"},
                {"start": "2026-04-01", "end": "2026-06-30", "val": 1000,
                 "filed": "2026-07-31", "form": "10-Q"},
            ]}}
        }}}
        with patch.object(app_module, "_sec_get_json", side_effect=[ticker_payload, facts_payload]):
            result = app_module._sec_reported_revenue_by_quarter("TEST")

        self.assertEqual(result, {"2026-06": 1000.0})
