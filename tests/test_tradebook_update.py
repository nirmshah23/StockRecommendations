import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from trade_recommendations import update_tradebook


class TradebookUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.book = self.root / "tradebook.csv"
        self.new = self.root / "new_trades.csv"

    def row(self, qty="5", date="2026-01-01", kind="buy", exchange="NSE", **extra):
        return dict(Symbol="ABC", TradeDate=date, Quantity=qty, Price="100",
                    Exchange=exchange, **{"Trade Type": kind, "Trade ID": "0012345678901234567"},
                    **extra)

    def write(self, buys, trades):
        pd.DataFrame(buys).to_csv(self.book, index=False)
        frame = pd.DataFrame(trades).rename(columns={
            "Symbol": "symbol", "TradeDate": "trade_date", "Quantity": "quantity",
            "Price": "price", "Trade Type": "trade_type", "Exchange": "exchange",
            "Trade ID": "trade_id"})
        frame.to_csv(self.new, index=False)

    def read(self):
        return pd.read_csv(self.book, dtype=str)

    def test_absent_import(self):
        update_tradebook(self.book, self.new)
        self.assertFalse(self.book.exists())

    def test_fifo_cross_exchange_and_partial_lot(self):
        self.write([self.row("4", "2026-01-02"), self.row("3", "2026-01-01")],
                   [self.row("5", "2026-02-01", "sell", "BSE")])
        original = self.book.read_bytes()
        update_tradebook(self.book, self.new)
        result = self.read()
        self.assertEqual(result.Quantity.tolist(), ["2"])
        self.assertEqual(result.TradeDate.tolist(), ["2026-01-02"])
        self.assertEqual(result["Trade ID"].iloc[0], "0012345678901234567")
        self.assertEqual((self.root / "tradebook.before_update.csv").read_bytes(), original)
        self.assertFalse(self.new.exists())

    def test_chronology_and_append(self):
        self.write([self.row("1")], [self.row("2", "2026-03-01", "sell"),
                                    self.row("3", "2026-02-01")])
        update_tradebook(self.book, self.new)
        self.assertEqual(self.read().Quantity.tolist(), ["2"])

    def test_oversell_and_future_buys_preserve_inputs(self):
        self.write([self.row("5", "2026-03-01")], [self.row("1", "2026-02-01", "sell")])
        before = (self.book.read_bytes(), self.new.read_bytes())
        with self.assertRaisesRegex(ValueError, "Insufficient holdings"):
            update_tradebook(self.book, self.new)
        self.assertEqual(before, (self.book.read_bytes(), self.new.read_bytes()))

    def test_invalid_inputs(self):
        for changes in [dict(Quantity="0"), dict(Quantity="NaN"), dict(Price="-1"),
                        dict(TradeDate="invalid"), {"Trade Type": "other"}]:
            with self.subTest(changes=changes):
                trade = self.row()
                trade.update(changes)
                self.write([self.row()], [trade])
                before = (self.book.read_bytes(), self.new.read_bytes())
                with self.assertRaises(ValueError):
                    update_tradebook(self.book, self.new)
                self.assertEqual(before, (self.book.read_bytes(), self.new.read_bytes()))

    def test_archive_date_and_collision(self):
        self.write([self.row()], [self.row()])
        with patch("trade_recommendations.d") as clock:
            clock.now.return_value.strftime.return_value = "2026-10-21"
            archive = self.root / "new_trades_added_2026-10-21.csv"
            archive.write_text("previous archive")
            imported = self.new.read_bytes()
            update_tradebook(self.book, self.new)
        self.assertEqual(archive.read_text(), "previous archive")
        self.assertEqual((self.root / "new_trades_added_2026-10-21_2.csv").read_bytes(), imported)

    def test_full_sale(self):
        self.write([self.row()], [self.row(kind="sell", date="2026-02-01")])
        update_tradebook(self.book, self.new)
        self.assertTrue(self.read().empty)

    def test_archive_failure_reports_committed_update(self):
        self.write([self.row()], [self.row()])
        original_open = Path.open

        def fail_archive(path, *args, **kwargs):
            if args and args[0] == "xb":
                raise PermissionError("archive denied")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", fail_archive):
            with self.assertRaisesRegex(RuntimeError, "Tradebook WAS updated"):
                update_tradebook(self.book, self.new)
        self.assertEqual(len(self.read()), 2)
        self.assertTrue(self.new.exists())


if __name__ == "__main__":
    unittest.main()
