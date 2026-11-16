import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from bhavAnalysis.download_bhavcopy import BhavcopyError, download_latest, fetch_day, parse_csv


def archive(day, csv_day=None):
    return ("SYMBOL, SERIES, DATE1, CLOSE_PRICE, DELIV_PER, PREV_CLOSE, DELIV_QTY\n"
            f"TEST, EQ, {csv_day or day.strftime('%d-%b-%Y')}, 100, 40, 100, 1000\n").encode()


class BhavcopyTests(unittest.TestCase):
    def test_skips_wrong_date_and_rejects_html(self):
        day = date(2026, 11, 13)
        with self.assertLogs(level="WARNING"):
            self.assertEqual(parse_csv(archive(day, "12-Nov-2026"), day), [])
        with self.assertRaises(BhavcopyError):
            parse_csv(b"<html>Access denied</html>", day)

    def test_fallback_host(self):
        day = date(2026, 11, 13)
        session = Mock()
        response = Mock(status_code=200, content=archive(day))
        session.get.side_effect = [requests.Timeout("timeout"), response]
        self.assertEqual(fetch_day(session, day, attempts=1), archive(day))
        self.assertIn("archives.nseindia.com", session.get.call_args.args[0])

    def test_outage_is_not_a_holiday(self):
        session = Mock()
        session.get.side_effect = requests.Timeout("offline")
        with self.assertRaises(BhavcopyError):
            fetch_day(session, date(2026, 11, 13), attempts=1)

    def test_missing_weekend_search_and_saved_files(self):
        friday = date(2026, 11, 13)
        with tempfile.TemporaryDirectory() as directory:
            with patch("bhavAnalysis.download_bhavcopy.fetch_day", side_effect=[None, None, archive(friday)]) as fetch:
                day, path = download_latest(directory, date(2026, 11, 15))
            self.assertEqual(day, friday)
            self.assertEqual(fetch.call_count, 3)
            self.assertTrue(path.exists())
            self.assertEqual(len(parse_csv(path.read_bytes(), friday)), 1)


if __name__ == "__main__":
    unittest.main()
