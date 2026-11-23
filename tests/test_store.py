import os
import tempfile
import unittest

from polycopy.store import Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "paper.db"))
        self.store.ensure_account(1000.0)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_buy_sell_settle(self):
        self.store.apply_buy(
            ts=1, target="t", signal_id=None, condition_id="c", token_id="k",
            outcome="Yes", title="m", event_slug="e", shares=10, price=0.5,
            fee=0.025,
        )
        acc = self.store.account()
        self.assertAlmostEqual(acc["cash"], 1000 - 5.025)
        pos = self.store.position("t", "c", "k")
        self.assertAlmostEqual(pos["shares"], 10)
        self.assertAlmostEqual(pos["avg_cost"], 0.5025)

        result = self.store.apply_sell(
            ts=2, target="t", signal_id=None, condition_id="c", token_id="k",
            outcome="Yes", shares=4, price=0.6, fee=0.0096,
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["realized"], 2.3904 - 4 * 0.5025)
        pos = self.store.position("t", "c", "k")
        self.assertAlmostEqual(pos["shares"], 6)

        realized = self.store.settle_position(pos, 1.0, ts=3)
        self.assertAlmostEqual(realized, 6 - 6 * 0.5025)
        closed = self.store.position("t", "c", "k")
        self.assertEqual(closed["status"], "CLOSED")
        self.assertAlmostEqual(closed["shares"], 0.0)
        acc = self.store.account()
        self.assertAlmostEqual(acc["cash"], 1000 - 5.025 + 2.3904 + 6.0)
        self.assertAlmostEqual(acc["realized_pnl"], 2.3904 - 4 * 0.5025 + 6 - 6 * 0.5025)
        self.assertEqual(self.store.count_open_positions(), 0)

    def test_target_ledger(self):
        self.store.add_target_shares("t", "c", "k", 100)
        self.assertAlmostEqual(self.store.target_shares("t", "k"), 100)
        self.store.add_target_shares("t", "c", "k", -40)
        self.assertAlmostEqual(self.store.target_shares("t", "k"), 60)
        self.store.add_target_shares("t", "c", "k", -100)
        self.assertAlmostEqual(self.store.target_shares("t", "k"), 0)

    def test_market_exposure(self):
        self.store.apply_buy(
            ts=1, target="t", signal_id=None, condition_id="c", token_id="k",
            outcome="Yes", title="m", event_slug="e", shares=10, price=0.5,
            fee=0.0,
        )
        self.assertAlmostEqual(self.store.market_exposure("c"), 5.0)


if __name__ == "__main__":
    unittest.main()
