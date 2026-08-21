from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import patch

from earnings_cal import app as app_module


class ArchivedEventTests(TestCase):
    def test_july_calendar_year_report_is_second_quarter(self):
        period = app_module._reported_fiscal_period(
            datetime(2026, 7, 24, 8), fiscal_year_end_month=12
        )
        self.assertEqual(period, "FY2026 Q2")

    def test_non_calendar_fiscal_year_is_labeled_correctly(self):
        period = app_module._reported_fiscal_period(
            datetime(2026, 7, 24, 8), fiscal_year_end_month=9
        )
        self.assertEqual(period, "FY2026 Q3")

    def test_projected_archived_date_is_not_promoted_to_past(self):
        archive = {
            "AVY": {
                "2026-07-21": {
                    "date": "2026-07-21T06:00:00-04:00",
                    "session": "BMO",
                    "eps_estimate": 2.45,
                    "eps_reported": None,
                    "eps_surprise_pct": None,
                }
            }
        }

        with patch.object(app_module, "_archive", archive):
            result = app_module._archived_past(
                "AVY", datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
            )

        self.assertEqual(result, [])

    def test_confirmed_archived_date_remains_in_history(self):
        confirmed = {
            "date": "2026-07-21T06:00:00-04:00",
            "session": "BMO",
            "eps_estimate": 2.45,
            "eps_reported": 2.51,
            "eps_surprise_pct": 2.45,
        }

        with patch.object(app_module, "_archive", {"SHW": {"2026-07-21": confirmed}}):
            result = app_module._archived_past(
                "SHW", datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
            )

        self.assertEqual(result, [confirmed])

    def test_nasdaq_actual_is_merged_even_with_future_event(self):
        result = {
            "ticker": "IFF",
            "upcoming": [{"date": "2026-11-03T16:00:00-05:00", "eps_reported": None}],
            "past": [],
        }
        nasdaq_actual = {
            "date": "2026-08-04T00:00:00-04:00",
            "session": None,
            "eps_estimate": 0.78,
            "eps_reported": 0.82,
            "eps_surprise_pct": 5.13,
            "revenue_estimate": None,
            "revenue_reported": None,
            "source": "Nasdaq earnings calendar",
        }
        now = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        with patch.object(app_module, "_nasdaq_window", return_value={"IFF": [nasdaq_actual]}):
            app_module._apply_nasdaq_fallback(result, now)

        self.assertEqual(len(result["upcoming"]), 1)
        self.assertEqual(result["past"][0]["eps_reported"], 0.82)
