import unittest

from polycopy.broker import Level, simulate_buy, simulate_sell, taker_fee
from polycopy.config import PaperConfig


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.cfg = PaperConfig()

    def test_taker_fee_formula(self):
        # fee = shares * rate * p * (1-p)
        self.assertAlmostEqual(taker_fee(100, 0.50, 0.01), 0.25)
        self.assertAlmostEqual(taker_fee(100, 0.30, 0.01), 0.21)
        self.assertAlmostEqual(taker_fee(100, 0.95, 0.01), 0.0475)

    def test_buy_walks_asks_ascending(self):
        book = ([], [Level(0.31, 10), Level(0.30, 10)])  # deliberately unsorted
        ex, reason = simulate_buy(book, 15, 0.30, self.cfg)
        self.assertIsNone(reason)
        self.assertAlmostEqual(ex.shares, 15)
        self.assertAlmostEqual(ex.price, (10 * 0.30 + 5 * 0.31) / 15)

    def test_buy_rejects_price_drift(self):
        book = ([], [Level(0.40, 100)])
        ex, reason = simulate_buy(book, 10, 0.30, self.cfg)
        self.assertIsNone(ex)
        self.assertEqual(reason, "drift")

    def test_buy_partial_depth(self):
        book = ([], [Level(0.30, 2)])
        ex, reason = simulate_buy(book, 10, 0.30, self.cfg)
        self.assertIsNone(ex)
        self.assertEqual(reason, "insufficient_depth")

    def test_sell_walks_bids_descending(self):
        book = ([Level(0.60, 5), Level(0.59, 10)], [])
        ex, reason = simulate_sell(book, 10, 0.60, self.cfg)
        self.assertIsNone(reason)
        self.assertAlmostEqual(ex.price, (5 * 0.60 + 5 * 0.59) / 10)

    def test_sell_rejects_price_drift(self):
        book = ([Level(0.40, 100)], [])
        ex, reason = simulate_sell(book, 10, 0.60, self.cfg)
        self.assertIsNone(ex)
        self.assertEqual(reason, "drift")


if __name__ == "__main__":
    unittest.main()
