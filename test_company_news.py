import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

import pandas as pd
import requests

from company_news import collect_company_news, fetch_news, news_days


def item(title, link, date, publisher='Publisher'):
    return (f'<item><title>{escape(title)}</title><link>{escape(link)}</link>'
            f'<pubDate>{date}</pubDate><source>{publisher}</source></item>')


class CompanyNewsTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_days_configuration(self):
        self.assertEqual(news_days(), 14)
        os.environ['NEWS_DAYS'] = '21'
        self.assertEqual(news_days(), 21)
        for value in ('0', '-1', 'abc', '366'):
            os.environ['NEWS_DAYS'] = value
            with self.assertRaises(ValueError):
                news_days()

    @patch.dict(os.environ, {'NEWS_SEARCH_TERMS': ' earnings, share price, ,earnings '})
    @patch('company_news.requests.get')
    def test_configured_terms_and_fourteen_day_cutoff(self, get):
        now = datetime.now(timezone.utc)
        entries = item('ABC earnings', 'https://example.com/recent', format_datetime(now-timedelta(days=13)))
        entries += item('ABC old', 'https://example.com/old', format_datetime(now-timedelta(days=15)))
        get.return_value.content = ('<rss><channel>'+entries+'</channel></rss>').encode()
        articles = fetch_news('ABC', 14, 10)
        self.assertEqual(len(articles), 1)
        query = parse_qs(urlsplit(get.call_args.args[0]).query)['q'][0]
        self.assertIn('("earnings" OR "share price")', query)
        self.assertIn('when:14d', query)
        with patch.dict(os.environ, {'NEWS_SEARCH_TERMS': ''}):
            fetch_news('ABC', 14, 10)
        query = parse_qs(urlsplit(get.call_args.args[0]).query)['q'][0]
        self.assertNotIn('()', query)
        self.assertNotIn('stock', query)

    @patch('company_news.requests.get')
    def test_feed_filters_deduplicates_sorts_and_limits(self, get):
        now = datetime.now(timezone.utc)
        recent = format_datetime(now - timedelta(hours=1))
        newer = format_datetime(now - timedelta(minutes=1))
        entries = [item('First - Publisher', 'https://example.com/1', recent),
                   item('Duplicate link', 'https://example.com/1', recent),
                   item('First - Other', 'https://example.com/2', recent, 'Other'),
                   item('Newest', 'https://example.com/3', newer),
                   item('Old', 'https://example.com/old', format_datetime(now-timedelta(days=8))),
                   item('Future', 'https://example.com/future', format_datetime(now+timedelta(days=1))),
                   item('Invalid', 'https://example.com/invalid', 'bad date'),
                   item('Unsafe', 'javascript:alert(1)', recent)]
        get.return_value.content = ('<rss><channel>'+''.join(entries)+'</channel></rss>').encode()
        articles = fetch_news('A & B Limited', 7, 10)
        self.assertEqual([a['Title'] for a in articles], ['Newest', 'First'])
        self.assertEqual(articles[1]['Publisher'], 'Publisher')
        self.assertEqual(len(fetch_news('A & B Limited', 7, 1)), 1)
        query = parse_qs(urlsplit(get.call_args.args[0]).query)
        self.assertIn('"A & B Limited"', query['q'][0])
        self.assertIn('when:7d', query['q'][0])
        self.assertEqual(query['hl'], ['en'])
        self.assertNotIn('gl', query)
        self.assertNotIn('ceid', query)
        self.assertEqual(get.call_args.kwargs['timeout'], 30)
        get.return_value.raise_for_status.assert_called()

    @patch('company_news.requests.get')
    def test_unrelated_headlines_are_excluded(self, get):
        date = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=1))
        entries = item('Other Cables shares', 'https://example.com/other', date)
        entries += item('Universal Cables earnings', 'https://example.com/right', date)
        get.return_value.content = ('<rss><channel>'+entries+'</channel></rss>').encode()
        articles = fetch_news('Universal Cables', 7, 10)
        self.assertEqual([a['Title'] for a in articles], ['Universal Cables earnings'])

    @patch('company_news.requests.get')
    def test_empty_malformed_and_http_failure(self, get):
        get.return_value.content = b'<rss><channel/></rss>'
        self.assertEqual(fetch_news('ABC', 7, 10), [])
        for content in (b'<html/>', b'<rss>'):
            get.return_value.content = content
            with self.assertRaises(ValueError if content == b'<html/>' else Exception):
                fetch_news('ABC', 7, 10)
        get.return_value.raise_for_status.side_effect = requests.HTTPError('503')
        with self.assertRaises(requests.HTTPError):
            fetch_news('ABC', 7, 10)

    @patch('company_news.fetch_news')
    def test_mapping_groups_and_failure_isolation(self, fetch):
        fetch.side_effect = [requests.Timeout('offline'), [], []]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mapping.csv'
            pd.DataFrame([dict(Name='ABC Limited', Symbol='ABC', BuyRSI='25')]).to_csv(path,index=False)
            original = path.read_bytes()
            groups = [('Buy', pd.DataFrame([dict(Company='ABC', Symbol='ABC')])),
                      ('Value', pd.DataFrame([dict(Company='ABC Limited'), dict(Company='New Co'), dict(Company='Other')]))]
            with self.assertLogs(level='ERROR'):
                results = collect_company_news(groups, path)
            self.assertEqual(len(results), 3)
            self.assertEqual(results[0]['Groups'], 'Buy, Value')
            self.assertIn('failed', results[0]['Status'])
            self.assertIn('No news', results[1]['Status'])
            self.assertEqual(fetch.call_args_list[0].args, ('ABC Limited', 14, 10))
            self.assertEqual(path.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
