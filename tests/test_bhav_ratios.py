import csv
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from bhavAnalysis.analysis import configuration, generate_reports, interpretation
from bhavAnalysis.download_bhavcopy import BhavcopyError, download_latest, parse_csv


class RatioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.start = date(2026, 11, 9)

    def source(self, offset, close=100, delivery=40, extra="", row_day=None, previous=100, quantity=None):
        day = self.start + timedelta(days=offset)
        path = self.root / f"sec_bhavdata_full_{day:%d%m%Y}.csv"
        path.write_text("SYMBOL, SERIES, DATE1, CLOSE_PRICE, DELIV_PER, PREV_CLOSE, DELIV_QTY\n"
                        f"ABC, EQ, {(row_day or day):%d-%b-%Y}, {close}, {delivery}, {previous}, {quantity if quantity is not None else close * 10}\n" + extra)
        return day

    @patch.dict(os.environ, {}, clear=True)
    def test_configuration(self):
        self.assertEqual(configuration(), (10, False, 0, 1, 1, 3))
        os.environ["BHAV_RECALCULATE_HISTORY"] = "TRUE"
        self.assertEqual(configuration(), (10, True, 0, 1, 1, 3))
        for value in ("0", "-1", "x", "1.5"):
            os.environ["BHAV_SMA_READINGS"] = value
            with self.assertRaises(ValueError):
                configuration()
        os.environ["BHAV_SMA_READINGS"] = "3"
        os.environ["BHAV_RECALCULATE_HISTORY"] = "yes"
        with self.assertRaises(ValueError):
            configuration()

    def test_interpretation_boundaries(self):
        from itertools import product
        for flags in product((False, True), repeat=3):
            values = [threshold + (1 if above else 0) for threshold, above in zip((2, 3, 4), flags)]
            expected = "Accumulation" if all(flags) else "Speculative Trading" if any(flags) else "Distribution"
            self.assertEqual(interpretation(*values, 2, 3, 4), expected)
        self.assertEqual(interpretation(-1, 0, 0), "Distribution")

    @patch.dict(os.environ, {}, clear=True)
    def test_report_settings_validation(self):
        for name, values in {
            "BHAV_PRICE_CHANGE_THRESHOLD": ("nan", "inf", "x"),
            "BHAV_DELIVERY_QUANTITY_THRESHOLD": ("0", "-1", "nan", "inf", "x"),
            "BHAV_DELIVERY_GROWTH_THRESHOLD": ("0", "-1", "nan", "inf", "x"),
            "BHAV_ACCUMULATION_DAYS": ("0", "-1", "1.5", "x"),
        }.items():
            for value in values:
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                    with self.assertRaisesRegex(ValueError, name):
                        configuration()
        with patch.dict(os.environ, {"BHAV_PRICE_CHANGE_THRESHOLD": "1.05", "BHAV_DELIVERY_GROWTH_THRESHOLD": "1.2", "BHAV_ACCUMULATION_DAYS": "5"}):
            self.assertEqual(configuration(), (10, False, 1.05, 1.2, 1, 5))

    def test_custom_threshold_and_consecutive_days_use_cached_ratios(self):
        for i in range(4):
            day = self.source(i, 100 + i * 10, 20 + i * 10)
        generate_reports(self.root, day, rebuild=True)
        history, report = generate_reports(self.root, day, price_threshold=1.05, delivery_threshold=1.05, days=2)
        self.assertEqual(len(report), 1)
        self.assertIn("Date2", report[0])
        self.assertNotIn("Date3", report[0])
        self.assertEqual(report[0]["PriceChangeThreshold"], 1.05)
        self.assertEqual(report[0]["ConsecutiveDays"], 2)
        old_price_sma = history[1]["PriceSMA"]
        history, report = generate_reports(self.root, day, price_threshold=100, delivery_threshold=2, days=2)
        self.assertEqual(report, [])
        self.assertEqual(history[1]["PriceSMA"], old_price_sma)
        self.assertEqual(history[1]["Interpretation"], "Speculative Trading")
        _, report = generate_reports(self.root, day, days=1)
        self.assertEqual(len(report), 1)
        _, report = generate_reports(self.root, day, days=5)
        self.assertEqual(report, [])
        self.assertIn("5 consecutive", (self.root / "accumulation_report.html").read_text(encoding="utf-8"))

    def test_independent_thresholds(self):
        for i in range(4):
            day = self.source(i, 100 + i * 10, 20 + i * 10)
        generate_reports(self.root, day, rebuild=True)
        _, report = generate_reports(self.root, day, price_threshold=1.01,
                                     delivery_threshold=1.1, days=2)
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["DeliveryGrowthThreshold"], 1.1)
        _, report = generate_reports(self.root, day, price_threshold=1.01,
                                     delivery_threshold=2, days=2)
        self.assertEqual(report, [])
        _, report = generate_reports(self.root, day, price_threshold=100,
                                     delivery_threshold=1.01, days=2)
        self.assertEqual(report, [])

    def test_partial_full_windows_and_no_future_leakage(self):
        for i in range(4):
            day = self.source(i, 100 + 10 * i, 20 + 10 * i)
        history, report = generate_reports(self.root, day, readings=2, rebuild=True, days=2)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["ReadingCount"], 2)
        self.assertEqual(history[0]["PriceSMA"], 105)
        self.assertEqual(history[0]["DeliverySMA"], 25)
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["Date1"], "2026-11-11")
        with (self.root / "accumulation_report.csv").open() as handle:
            saved = list(csv.DictReader(handle))
        self.assertEqual(len(saved), 1)
        self.assertIn(str(report[0]["PriceChangePct2"]), (self.root / "accumulation_report.html").read_text(encoding="utf-8"))

    def test_bounded_recalculation_preserves_older_results(self):
        for i in range(5):
            day = self.source(i, 100 + i * 10, 20 + i * 10)
        generate_reports(self.root, day, rebuild=True)
        self.source(0, close=50)
        self.source(3, close=200)
        history, _ = generate_reports(self.root, day, readings=2, rebuild=True)
        self.assertEqual(len(history), 5)
        self.assertEqual(history[0]["Close"], "100.0")
        self.assertEqual(history[2]["SMAReadings"], "10")
        self.assertEqual(history[3]["Close"], 200)
        self.assertEqual(history[3]["PriceSMA"], 115)
        self.assertEqual(history[4]["PriceSMA"], 160)
        self.assertEqual(history[3]["SMAReadings"], 2)

    def test_incremental_replacement_and_rebuild(self):
        for i in range(4):
            day = self.source(i, 100 + 10 * i, 20 + 10 * i)
        history, report = generate_reports(self.root, day)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["ReadingCount"], 3)
        self.assertEqual(report, [])
        generate_reports(self.root, day, rebuild=True)
        self.source(0, close=50)
        history, _ = generate_reports(self.root, day, readings=2)
        self.assertEqual(history[0]["Close"], "100.0")  # Older ratios untouched.
        self.assertEqual(history[-1]["SMAReadings"], 2)
        self.source(3, delivery="-")
        history, report = generate_reports(self.root, day)
        self.assertEqual(len(history), 3)  # Previously calculated latest row removed.
        self.assertEqual(report, [])
        history, _ = generate_reports(self.root, day, rebuild=True)
        self.assertEqual(history[0]["Close"], 50)

    def test_missing_delivery_series_and_date_rows(self):
        self.source(0, delivery="-")
        self.source(1, close=110, delivery=30, row_day=self.start)
        day = self.source(2, close=120, delivery=40,
                          extra="ABC, BE, 11-Nov-2026, 500, 80, 100, 1000\n")
        with self.assertLogs(level="WARNING"):
            history, report = generate_reports(self.root, day, rebuild=True)
        self.assertEqual(len(history), 2)
        self.assertEqual([r["ReadingCount"] for r in history], [0, 0])
        self.assertEqual(report, [])
        self.assertIn("No qualifying stocks", (self.root / "accumulation_report.html").read_text(encoding="utf-8"))

    def test_mixed_dates_keep_matching_rows(self):
        day = self.source(0, extra="XYZ, EQ, 08-Nov-2026, 200, 50, 100, 1000\n")
        with self.assertLogs(level="WARNING"):
            history, _ = generate_reports(self.root, day)
        self.assertEqual([row["Symbol"] for row in history], ["ABC"])

    def test_excluded_latest_date_breaks_three_date_streak(self):
        for i in range(5):
            day = self.source(i, close=100 + i * 10, delivery=20 + i * 10)
        generate_reports(self.root, day, rebuild=True)
        # Do not fall back to an older valid date if a saved session is excluded.
        self.source(3, delivery="-")
        with self.assertLogs(level="WARNING"):
            _, report = generate_reports(self.root, day, rebuild=True)
        self.assertEqual(report, [])

    def test_future_source_not_used_for_current_average(self):
        day = self.source(0)
        self.source(1, close=1000, delivery=100)
        history, _ = generate_reports(self.root, day)
        self.assertEqual(history[0]["PriceSMA"], "")
        self.assertEqual(history[0]["ReadingCount"], 0)

    def test_invalid_values_and_zero_average(self):
        self.source(0, close="nan")
        self.source(1, delivery=101)
        day = self.source(2, delivery=0)
        with self.assertLogs(level="WARNING"):
            history, _ = generate_reports(self.root, day, rebuild=True)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["DeliveryGrowth"], "")
        self.assertEqual(history[0]["Interpretation"], "")

    def test_duplicate_rejected_and_failed_calculation_preserves_reports(self):
        day = self.source(0)
        generate_reports(self.root, day)
        before = (self.root / "analysis_history.csv").read_bytes()
        self.source(0, extra="ABC, EQ, 09-Nov-2026, 200, 50, 100, 1000\n")
        with self.assertRaises(BhavcopyError):
            generate_reports(self.root, day)
        self.assertEqual((self.root / "analysis_history.csv").read_bytes(), before)

    def test_price_change_percentage_and_previous_close_validation(self):
        for previous, close, expected in [(100, 110, 10), (100, 90, -10), (100, 100, 0),
                                           (200, 210, 5)]:
            with self.subTest(previous=previous, close=close):
                day = self.source(0, close=close, previous=previous)
                history, _ = generate_reports(self.root, day)
                self.assertAlmostEqual(history[0]["PriceChangePct"], expected)
                self.assertEqual(history[0]["PreviousClose"], previous)
        for previous in (0, -1, "nan", "inf", "-", ""):
            with self.subTest(previous=previous), self.assertLogs(level="WARNING"):
                day = self.source(0, close=110, previous=previous)
                history, report = generate_reports(self.root, day)
                self.assertEqual(history[0]["PriceChangePct"], "")
                self.assertEqual(history[0]["Interpretation"], "")
                self.assertEqual(history[0]["PriceSMA"], "")
                self.assertEqual(report, [])

    @patch.dict(os.environ, {}, clear=True)
    def test_percentage_thresholds_and_ignored_old_setting(self):
        os.environ["BHAV_PRICE_GROWTH_THRESHOLD"] = "99"
        self.assertEqual(configuration()[2], 0)
        for value in (0, -2, 2):
            os.environ["BHAV_PRICE_CHANGE_THRESHOLD"] = str(value)
            self.assertEqual(configuration()[2], value)
        self.assertEqual(interpretation(-1, 2, 2, -2), "Accumulation")
        self.assertEqual(interpretation(0, 2, 2), "Speculative Trading")
        day = self.source(0, close=102, previous=100)
        _, report = generate_reports(self.root, day, days=1, price_threshold=2, delivery_threshold=0.5)
        self.assertEqual(report, [])

    def test_history_upgrade_recalculates_recent_dates_only(self):
        from bhavAnalysis.analysis import LEGACY_FIELDS, PREVIOUS_FIELDS, csv_bytes
        for i in range(5):
            day = self.source(i, close=110 + i * 10, delivery=20 + i * 10)
        history, _ = generate_reports(self.root, day, rebuild=True)
        for schema in (LEGACY_FIELDS, PREVIOUS_FIELDS):
            legacy = [{field: (999 if field == "PriceGrowth" else row[field]) for field in schema}
                      for row in history]
            (self.root / "analysis_history.csv").write_bytes(csv_bytes(schema, legacy))
            with self.assertLogs(level="WARNING"):
                migrated, report = generate_reports(self.root, day, readings=2, days=2)
            self.assertEqual(len(migrated), 5)
            self.assertEqual(migrated[2]["Interpretation"], "")
            self.assertEqual(migrated[2]["DeliveryQuantity"], "")
            self.assertEqual(float(migrated[2]["PriceSMA"]), history[2]["PriceSMA"])
            self.assertEqual(migrated[3]["PriceSMA"], 125)
            self.assertAlmostEqual(migrated[3]["DeliveryQuantity"], 1400 / 1250)
            self.assertEqual(len(report), 1)
            self.assertEqual(report[0]["DeliveryQuantityThreshold"], 1)
            self.assertIn("DeliveryQuantity2", (self.root / "accumulation_report.html").read_text())
            migrated, _ = generate_reports(self.root, day, readings=2)
            self.assertEqual(migrated[2]["Interpretation"], "")

    def test_quantity_prior_windows_invalid_values_and_zero_denominator(self):
        self.source(0, quantity=100)
        self.source(1, quantity="-", close=110)
        day = self.source(2, quantity=300, close=120)
        history, _ = generate_reports(self.root, day, rebuild=True)
        self.assertEqual(history[1]["DeliveryQuantity"], "")
        self.assertEqual(history[1]["Interpretation"], "")
        self.assertEqual(history[2]["DeliveryQuantity"], 3)
        self.assertEqual(history[2]["DeliveryQuantityReadingCount"], 1)
        self.assertEqual(history[2]["ReadingCount"], 2)
        self.assertEqual(history[2]["PriceSMA"], 105)
        # A one-reading window uses yesterday, never the current value.
        history, _ = generate_reports(self.root, day, readings=1)
        self.assertEqual(history[-1]["PriceSMA"], 110)
        self.assertEqual(history[-1]["DeliveryQuantity"], 3)
        self.source(0, quantity=0, delivery=0)
        self.source(1, quantity=0, delivery=0)
        history, _ = generate_reports(self.root, day, rebuild=True)
        self.assertEqual(history[-1]["DeliveryQuantity"], "")
        self.assertEqual(history[-1]["DeliveryGrowth"], "")
        self.assertEqual(history[-1]["Interpretation"], "")
        for bad in ("nan", "inf", -1, ""):
            day = self.source(2, quantity=bad)
            history, _ = generate_reports(self.root, day)
            self.assertEqual(history[-1]["DeliveryQuantityValue"], "")
            self.assertEqual(history[-1]["Interpretation"], "")

    def test_quantity_threshold_reclassifies_cached_metrics(self):
        for i in range(4):
            day = self.source(i, close=110 + i * 10, delivery=20 + i * 10)
        history, report = generate_reports(self.root, day, rebuild=True, days=2)
        self.assertEqual(len(report), 1)
        old_sma = history[2]["DeliveryQuantitySMA"]
        history, report = generate_reports(self.root, day, quantity_threshold=2, days=2)
        self.assertEqual(report, [])
        self.assertEqual(history[2]["Interpretation"], "Speculative Trading")
        self.assertEqual(float(history[2]["DeliveryQuantitySMA"]), old_sma)

    def test_previous_close_column_required_and_unknown_history_rejected(self):
        day = self.source(0)
        path = next(self.root.glob("sec_*.csv"))
        with self.assertRaises(BhavcopyError):
            parse_csv(path.read_bytes().replace(b"PREV_CLOSE", b"OTHER"), day)
        with self.assertRaises(BhavcopyError):
            parse_csv(path.read_bytes().replace(b"DELIV_QTY", b"OTHER"), day)
        (self.root / "analysis_history.csv").write_text("Unknown\nvalue\n")
        with self.assertRaisesRegex(BhavcopyError, "Invalid history schema"):
            generate_reports(self.root, day)


    def test_download_overrides_existing_and_failure_preserves_source(self):
        day = self.source(0)
        path = next(self.root.glob("sec_*.csv"))
        original = path.read_bytes()
        replacement = original.replace(b"100", b"200")
        with patch("bhavAnalysis.download_bhavcopy.fetch_day", return_value=replacement):
            download_latest(self.root, day)
        self.assertEqual(path.read_bytes(), replacement)
        with patch("bhavAnalysis.download_bhavcopy.fetch_day", return_value=b"bad"):
            with self.assertRaises(BhavcopyError):
                download_latest(self.root, day)
        self.assertEqual(path.read_bytes(), replacement)


if __name__ == "__main__":
    unittest.main()
