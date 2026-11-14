import io
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from bhavAnalysis.download_bhavcopy import BhavcopyError, download_latest, fetch_day, validate_zip


def archive(day, csv_day=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        handle.writestr(
            f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv",
            f"TradDt,TckrSymb,ClsPric\n{csv_day or day.isoformat()},TEST,100\n",
        )
    return buffer.getvalue()


class BhavcopyTests(unittest.TestCase):
    def test_rejects_wrong_date_and_html(self):
        day = date(2026, 11, 13)
        for content in (archive(day, "2026-11-12"), b"<html>Access denied</html>"):
            with self.assertRaises(BhavcopyError):
                validate_zip(content, day)

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
            validate_zip(Path(str(path) + ".zip").read_bytes(), friday)


if __name__ == "__main__":
    unittest.main()
