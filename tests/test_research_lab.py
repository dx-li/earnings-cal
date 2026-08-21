from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from earnings_cal.research_sources import _quarterly_facts, qualitative_signals
from earnings_cal.research_store import ResearchStore
from earnings_cal.research_agents import ResearchPipeline, _deepseek_extract, standardized_call
from earnings_cal.research_config import ResearchConfig
from unittest.mock import patch
from earnings_cal import research_sources
from earnings_cal.earnings_brief import EarningsBriefHarness


class ResearchLabTests(TestCase):
    def test_earnings_brief_rejects_uncited_output(self):
        with TemporaryDirectory() as tmp:
            harness = EarningsBriefHarness(Path(tmp), "test-key")
            with self.assertRaises(ValueError):
                harness._validate({"period":"Q1","verdict":"","top_line":{},"bottom_line":{},"cash_inventory":{},"watch_items":[],"sources":[],"limitations":[]})

    def test_earnings_brief_cache_round_trip(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ABC-latest-brief.json"
            path.write_text('{"ticker":"ABC"}', encoding="utf-8")
            self.assertEqual(EarningsBriefHarness(Path(tmp), "test-key").cached("abc"), {"ticker":"ABC"})

    def test_store_is_append_only_for_observations(self):
        with TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp))
            kpi = store.add_kpi("ABC", {"name": "Volume", "unit": "tons"})
            store.add_observation("ABC", {"kpi_id": kpi["id"], "period": "2026 Q2", "value": 10})
            store.add_observation("ABC", {"kpi_id": kpi["id"], "period": "2026 Q2", "value": 11})
            self.assertEqual(len(store.load("ABC")["observations"]), 2)

    def test_quarterly_facts_excludes_year_to_date(self):
        facts = {"us-gaap": {"Revenues": {"units": {"USD": [
            {"start":"2026-01-01","end":"2026-06-30","val":200,"form":"10-Q","fp":"Q2","filed":"2026-07-20"},
            {"start":"2026-04-01","end":"2026-06-30","val":110,"form":"10-Q","fp":"Q2","filed":"2026-07-20"},
        ]}}}}
        rows = _quarterly_facts(facts, ["Revenues"])
        self.assertEqual(rows[0]["value"], 110)

    def test_qualitative_signals_are_repeatable(self):
        signals = qualitative_signals("Strong pricing and volume growth, but margin pressure and destocking.")
        self.assertGreater(signals["themes"]["pricing"], 0)
        self.assertGreater(signals["negative_mentions"], 0)

    def test_call_taxonomy_is_stable_across_names(self):
        result = standardized_call("Demand improved. Pricing was strong. Inventory destocking continued.")
        self.assertEqual(result["taxonomy_version"], "1.0")
        self.assertGreater(result["categories"]["demand"]["mentions"], 0)
        self.assertGreater(result["categories"]["inventory"]["mentions"], 0)

    def test_accepting_segment_change_creates_lineage_edge(self):
        with TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp))
            data = store.normalize("ABC")
            data["review_queue"].append({
                "id":"r1", "kind":"segment_lineage", "status":"pending",
                "proposal":{"name":"New Materials", "effective_period":"2026 Q1", "status":"active",
                            "predecessors":["Legacy Materials"], "change_type":"reorganization",
                            "source_quote":"We reorganized the segment.", "source_url":"https://www.sec.gov/Archives/test"},
            })
            store.save("ABC", data)
            ResearchPipeline(store).decide("ABC", "r1", "accepted")
            saved = store.load("ABC")
            self.assertEqual(saved["segments"][0]["name"], "New Materials")
            self.assertEqual(saved["segment_edges"][0]["from_name"], "Legacy Materials")

    def test_config_round_trips_protected_secret(self):
        with TemporaryDirectory() as tmp:
            cfg = ResearchConfig(Path(tmp))
            saved = cfg.save({"start_year": 2000, "openai_api_key": "secret-test-key"})
            self.assertTrue(saved["openai_api_key_configured"])
            self.assertEqual(cfg.secrets()["openai_api_key"], "secret-test-key")
            self.assertNotIn("secret-test-key", cfg.secrets_path.read_text())

    def test_config_round_trips_deepseek_provider_and_secret(self):
        with TemporaryDirectory() as tmp:
            cfg = ResearchConfig(Path(tmp))
            saved = cfg.save({"provider":"deepseek", "model":"deepseek-v4-flash", "deepseek_api_key":"deepseek-secret"})
            self.assertEqual(saved["provider"], "deepseek")
            self.assertTrue(saved["deepseek_api_key_configured"])
            self.assertEqual(cfg.secrets()["deepseek_api_key"], "deepseek-secret")

    @patch("earnings_cal.research_agents.requests.post")
    def test_deepseek_adapter_normalizes_json_and_usage(self, post):
        post.return_value.json.return_value = {"choices":[{"message":{"content":"{\"kpis\":[],\"segments\":[],\"themes\":[]}"}}],"usage":{"prompt_tokens":100,"completion_tokens":20}}
        result, usage = _deepseek_extract("filing", "ABC", "https://example.test", {"model":"deepseek-v4-flash"}, {"deepseek_api_key":"secret"})
        self.assertEqual(result["kpis"], [])
        self.assertEqual(usage, {"input_tokens":100,"output_tokens":20})
        self.assertEqual(post.call_args.args[0], "https://api.deepseek.com/chat/completions")

    def test_historical_filing_shards_are_included(self):
        current = {"filings":{"recent":{"form":[],"accessionNumber":[],"primaryDocument":[],"filingDate":[],"reportDate":[]},"files":[{"name":"old.json","filingFrom":"2000-01-01","filingTo":"2001-12-31"}]}}
        old = {"form":["10-K"],"accessionNumber":["0001-01-000001"],"primaryDocument":["annual.htm"],"filingDate":["2001-03-01"],"reportDate":["2000-12-31"],"primaryDocDescription":["Annual report"]}
        with patch.object(research_sources, "cik_for_ticker", return_value=1), patch.object(research_sources, "_get", side_effect=[current, old]):
            rows = research_sources.filings("ABC", start_year=2000)
        self.assertEqual(rows[0]["filed"], "2001-03-01")
