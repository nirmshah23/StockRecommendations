import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from company_news import collect_company_news, news_html, resolve_ticker


class CompanyNewsTests(unittest.TestCase):
    @patch("company_news.yf.Search")
    def test_sme_identifier_matches_screener(self, search):
        search.return_value.quotes = [dict(symbol="UNIHEALTH-SM.NS", exchange="NSI",
                                          shortname="UNIHEALTH HOSPITALS LTD")]
        self.assertEqual(resolve_ticker("Unihealth Hosp", "UNIHEALTH"), "UNIHEALTH-SM.NS")

    @patch("company_news.yf.Search")
    def test_indian_matching_and_nse_preference(self, search):
        search.return_value.quotes = [
            dict(symbol="ABC", exchange="NYQ", longname="ABC Limited"),
            dict(symbol="ABC.BO", exchange="BSE", longname="ABC Limited"),
            dict(symbol="ABC.NS", exchange="NSI", longname="ABC Limited"),
            dict(symbol="OTHER.NS", exchange="NSI", longname="Other Limited")]
        self.assertEqual(resolve_ticker("ABC Ltd"), "ABC.NS")
        self.assertIsNone(resolve_ticker("Unknown Ltd"))

    @patch("company_news.yf.Search")
    def test_ambiguous_names_not_cached(self, search):
        search.return_value.quotes = [
            dict(symbol="A.NS", exchange="NSI", longname="ABC Limited"),
            dict(symbol="B.NS", exchange="NSI", longname="ABC Limited")]
        self.assertIsNone(resolve_ticker("ABC"))

    @patch("company_news.yf.Ticker")
    @patch("company_news.yf.Search")
    def test_cache_dedup_recent_news_and_new_screen_company(self, search, ticker):
        search.return_value.quotes = [dict(symbol="ABC.NS", exchange="NSI", longname="ABC Limited")]
        article = dict(title="A <headline>", providerPublishTime=int(datetime.now(timezone.utc).timestamp()),
                       link="https://example.com/news", publisher="Example")
        search.return_value.news = [article, article,
            dict(title="Old", providerPublishTime=1577836800, link="https://example.com/old")]
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "mapping.csv"
            pd.DataFrame([dict(Name="Existing", Symbol="EXIST", BuyRSI="25", SellRSI="75")]).to_csv(path, index=False)
            frame = pd.DataFrame([dict(Company="ABC Limited")])
            results = collect_company_news([("Dividend", frame), ("Value", frame)], path)
            self.assertEqual(len(results), 1)
            self.assertEqual(len(results[0]["Articles"]), 1)
            self.assertEqual(results[0]["Groups"], "Dividend, Value")
            self.assertEqual(results[0]["Articles"][0]["Publisher"], "Example")
            search.assert_any_call("ABC.NS", max_results=0, news_count=10)
            ticker.assert_not_called()
            self.assertIn("&lt;headline&gt;", news_html(results))
            cache = pd.read_csv(path, keep_default_na=False)
            self.assertEqual(cache.loc[0, "BuyRSI"], "25")
            self.assertEqual(cache.loc[1, "YahooTicker"], "ABC.NS")
            search.reset_mock()
            collect_company_news([("Dividend", frame)], path, count=5)
            search.assert_called_once_with("ABC.NS", max_results=0, news_count=5)

    @patch("company_news.resolve_ticker", side_effect=RuntimeError("offline"))
    def test_lookup_failure_is_reported(self, resolve):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "mapping.csv"
            pd.DataFrame([dict(Name="ABC", Symbol="ABC")]).to_csv(path, index=False)
            results = collect_company_news([("Buy", pd.DataFrame([dict(Company="ABC")]))], path)
            self.assertIn("failed", results[0]["Status"])


if __name__ == "__main__":
    unittest.main()
