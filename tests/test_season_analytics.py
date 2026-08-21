from unittest import TestCase

from earnings_cal.season_analytics import summarize_seasons


class SeasonAnalyticsTests(TestCase):
    def test_rates_reactions_and_oracle(self):
        companies = [{"ticker": "A", "past": [
            {"date": "2026-07-20T08:00:00-04:00", "eps_estimate": 1, "eps_reported": 2,
             "revenue_estimate": 10, "revenue_reported": 12, "move_day": 5,
             "move_before": 1, "move_after": 2, "relative_move_day": 4},
            {"date": "2026-08-01T08:00:00-04:00", "eps_estimate": 2, "eps_reported": 1,
             "revenue_estimate": 10, "revenue_reported": 9, "move_day": -3,
             "move_before": -1, "move_after": -2, "relative_move_day": -4},
            {"date": "2026-04-20T08:00:00-04:00", "eps_estimate": 1, "eps_reported": 2,
             "revenue_estimate": None, "revenue_reported": 12, "move_day": 1},
        ]}]
        result = summarize_seasons(companies)
        current = result["current"]
        self.assertEqual(current["quarter"], "2026 Q3")
        self.assertEqual(current["eps"]["beat"]["pct"], 50)
        self.assertEqual(current["double_beat"]["pct"], 50)
        self.assertEqual(current["reaction"]["eps_beat"]["average"], 5)
        self.assertEqual(current["windows"]["eps_beat"]["before"]["average"], 1)
        self.assertEqual(current["windows"]["eps_miss"]["after"]["average"], -2)
        self.assertEqual(current["oracle_eps"]["average"], 4)
        self.assertEqual(result["previous"]["quarter"], "2026 Q2")
        self.assertEqual([season["quarter"] for season in result["history"]], ["2026 Q2", "2026 Q3"])

    def test_missing_consensus_is_excluded_from_denominator(self):
        result = summarize_seasons([{"past": [{
            "date": "2026-01-01T08:00:00Z", "eps_estimate": None,
            "eps_reported": 2, "move_day": 1,
        }]}])
        self.assertEqual(result["current"]["eps"]["beat"]["n"], 0)
        self.assertIsNone(result["current"]["eps"]["beat"]["pct"])
