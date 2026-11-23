import unittest

from polycopy.broker import (
    Book, Level, buy_cap, parse_book, sell_floor, simulate_buy, simulate_sell,
    taker_fee,
)
from polycopy.config import PaperConfig


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.cfg = PaperConfig()

    def test_taker_fee_formula(self):
        # fee = shares * rate * p * (1-p)
        self.assertAlmostEqual(taker_fee(100, 0.50, 0.01), 0.25)
        self.assertAlmostEqual(taker_fee(100, 0.30, 0.01), 0.21)
        self.assertAlmostEqual(taker_fee(100, 0.95, 0.01), 0.0475)
        # Current Polymarket crypto taker rate: 0.07.
        self.assertAlmostEqual(taker_fee(100, 0.50, 0.07), 1.75)

    def test_parse_book_reads_tick_size(self):
        book = parse_book({
            "bids": [{"price": "0.49", "size": "10"}],
            "asks": [{"price": "0.51", "size": "10"}],
            "tick_size": "0.001",
        })
        self.assertIsInstance(book, Book)
        self.assertAlmostEqual(book.tick_size, 0.001)
        self.assertEqual(book.bids[0].price, 0.49)
        self.assertEqual(book.asks[0].price, 0.51)

    def test_buy_walks_asks_ascending(self):
        book = Book(bids=(), asks=(Level(0.31, 10), Level(0.30, 10)))
        ex, reason = simulate_buy(book, 15, 0.30, self.cfg)
        self.assertIsNone(reason)
        self.assertAlmostEqual(ex.shares, 15)
        self.assertAlmostEqual(ex.price, (10 * 0.30 + 5 * 0.31) / 15)

    def test_buy_rejects_price_drift(self):
        book = Book(bids=(), asks=(Level(0.40, 100),))
        ex, reason = simulate_buy(book, 10, 0.30, self.cfg)
        self.assertIsNone(ex)
        self.assertEqual(reason, "drift")

    def test_buy_partial_depth(self):
        book = Book(bids=(), asks=(Level(0.30, 2),))
        ex, reason = simulate_buy(book, 10, 0.30, self.cfg)
        self.assertIsNone(ex)
        self.assertEqual(reason, "insufficient_depth")

    def test_cheap_token_relative_cap_blocks_10x_fill(self):
        # signal 0.003: absolute cap would allow 0.033, relative cap only 0.004
        self.assertAlmostEqual(buy_cap(0.003, self.cfg, 0.001), 0.004)
        book = Book(bids=(), asks=(Level(0.010, 100_000),), tick_size=0.001)
        ex, reason = simulate_buy(book, 1000, 0.003, self.cfg)
        self.assertIsNone(ex)
        self.assertEqual(reason, "drift")

    def test_cheap_token_one_tick_fill_allowed(self):
        book = Book(bids=(), asks=(Level(0.004, 100_000),), tick_size=0.001)
        ex, reason = simulate_buy(book, 1000, 0.003, self.cfg)
        self.assertIsNone(reason)
        self.assertAlmostEqual(ex.price, 0.004)

    def test_normal_price_uses_absolute_cap(self):
        # 0.50 + 0.03 = 0.53; relative 10% would be 0.55, so absolute wins
        self.assertAlmostEqual(buy_cap(0.50, self.cfg, 0.001), 0.53)
        book = Book(bids=(), asks=(Level(0.53, 100),))
        ex, reason = simulate_buy(book, 10, 0.50, self.cfg)
        self.assertIsNone(reason)
        book2 = Book(bids=(), asks=(Level(0.54, 100),))
        ex2, reason2 = simulate_buy(book2, 10, 0.50, self.cfg)
        self.assertIsNone(ex2)
        self.assertEqual(reason2, "drift")

    def test_sell_walks_bids_descending(self):
        book = Book(bids=(Level(0.60, 5), Level(0.59, 10)), asks=())
        ex, reason = simulate_sell(book, 10, 0.60, self.cfg)
        self.assertIsNone(reason)
        self.assertAlmostEqual(ex.price, (5 * 0.60 + 5 * 0.59) / 10)

    def test_sell_rejects_price_drift(self):
        book = Book(bids=(Level(0.40, 100),), asks=())
        ex, reason = simulate_sell(book, 10, 0.60, self.cfg)
        self.assertIsNone(ex)
        self.assertEqual(reason, "drift")

    def test_sell_floor_cheap_token(self):
        # 0.003 - 15% = 0.00255, but one tick is 0.002 -> floor 0.002
        self.assertAlmostEqual(sell_floor(0.003, self.cfg, 0.001), 0.002)


if __name__ == "__main__":
    unittest.main()
