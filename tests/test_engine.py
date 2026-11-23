import os
import tempfile
import time
import unittest

from polycopy.config import AccountConfig, AppConfig, PaperConfig, TargetConfig
from polycopy.engine import Engine
from polycopy.store import Store


class FakeData:
    def __init__(self, rows):
        self.rows = rows

    def activity(self, user, start_ts, limit=500, max_pages=3):
        return list(self.rows)


class FakeClob:
    def __init__(self, bid=0.59, ask=0.50):
        self.bid = bid
        self.ask = ask

    def book(self, token):
        return {
            "bids": [{"price": str(self.bid), "size": "1000"}],
            "asks": [{"price": str(self.ask), "size": "1000"}],
        }


class FakeGamma:
    def __init__(self, closed=False, yes_wins=True):
        self.closed = closed
        self.yes_wins = yes_wins

    def market_by_condition(self, cid):
        if self.closed:
            prices = '["1", "0"]' if self.yes_wins else '["0", "1"]'
            return {
                "closed": True,
                "outcomes": '["Yes", "No"]',
                "outcomePrices": prices,
                "clobTokenIds": '["t1", "t2"]',
            }
        return {
            "closed": False,
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.5", "0.5"]',
            "clobTokenIds": '["t1", "t2"]',
        }


def trade_row(side, size, price, ts, tx="0x1"):
    return {
        "type": "TRADE",
        "transaction_hash": tx,
        "condition_id": "c1",
        "token_id": "t1",
        "side": side,
        "price": price,
        "size": size,
        "usdc_size": size * price,
        "timestamp": ts,
        "outcome": "Yes",
        "title": "Test market",
        "slug": "test-market",
        "event_slug": "test-event",
    }


class EngineTest(unittest.TestCase):
    def make_engine(self, rows, gamma=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = os.path.join(tmp.name, "paper.db")
        cfg = AppConfig(
            account=AccountConfig(starting_cash=1000.0),
            paper=PaperConfig(
                db_path=db,
                log_path=os.path.join(tmp.name, "paper.log"),
                settle_every_seconds=10 ** 12,
                mark_every_seconds=10 ** 12,
                min_shares=1.0,
                fee_rate=0.01,
            ),
            targets={
                "t": TargetConfig(
                    address="0xabc",
                    scale_factor=0.02,
                    min_notional_usd=2.0,
                    max_notional_usd=100.0,
                    min_target_usd=20.0,
                    buy_price_min=0.30,
                    buy_price_max=0.70,
                    max_market_usd=1000.0,
                    max_open_positions=10,
                )
            },
        )
        store = Store(db)
        self.addCleanup(store.close)
        store.ensure_account(1000.0)
        engine = Engine(cfg, store, FakeData(rows), FakeClob(), gamma or FakeGamma())
        return engine, store

    def test_copy_buy_and_proportional_sell(self):
        now = int(time.time())
        rows = [
            trade_row("BUY", 100, 0.50, now - 5, tx="0x1"),
            trade_row("SELL", 40, 0.60, now - 4, tx="0x2"),
        ]
        engine, store = self.make_engine(rows)
        engine.cycle()

        pos = store.position("0xabc", "c1", "t1")
        self.assertEqual(pos["status"], "OPEN")
        # bought $2 of a $50 target fill -> 4 shares, then target sold 40% -> 1.6
        self.assertAlmostEqual(pos["shares"], 2.4)

        fee_sell = 1.6 * 0.59 * (1 - 0.59) * 0.01
        expected_cash = 1000.0 - 2.0 - 0.01 + 1.6 * 0.59 - fee_sell
        self.assertAlmostEqual(store.account()["cash"], expected_cash, places=6)

        total, copied = store.signal_counts()
        self.assertEqual(total, 2)
        self.assertEqual(copied, 2)

    def test_price_filter_records_skip(self):
        now = int(time.time())
        rows = [trade_row("BUY", 100, 0.80, now - 5)]
        engine, store = self.make_engine(rows)
        engine.cycle()
        self.assertEqual(store.count_open_positions(), 0)
        summary = store.signal_summary()
        self.assertEqual(summary[0]["reason"], "price_out_of_range")
        self.assertEqual(summary[0]["copied"], 0)

    def test_settlement_pays_winner(self):
        now = int(time.time())
        rows = [trade_row("BUY", 100, 0.50, now - 5)]
        engine, store = self.make_engine(rows, gamma=FakeGamma(closed=True))
        engine.cycle()
        settled = engine.settle()
        self.assertEqual(settled, 1)
        self.assertEqual(store.count_open_positions(), 0)
        self.assertAlmostEqual(store.account()["cash"], 1000.0 - 2.01 + 4.0)

    def test_settlement_loser_pays_zero(self):
        now = int(time.time())
        rows = [trade_row("BUY", 100, 0.50, now - 5)]
        engine, store = self.make_engine(rows, gamma=FakeGamma(closed=True, yes_wins=False))
        engine.cycle()
        engine.settle()
        self.assertEqual(store.count_open_positions(), 0)
        self.assertAlmostEqual(store.account()["cash"], 1000.0 - 2.01)

    def test_second_cycle_does_not_duplicate(self):
        now = int(time.time())
        rows = [trade_row("BUY", 100, 0.50, now - 5)]
        engine, store = self.make_engine(rows)
        engine.cycle()
        engine.cycle()
        total, copied = store.signal_counts()
        self.assertEqual(total, 1)
        self.assertEqual(copied, 1)
        self.assertAlmostEqual(store.position("0xabc", "c1", "t1")["shares"], 4.0)


if __name__ == "__main__":
    unittest.main()
