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

    def test_pending_exit_caps_and_clears(self):
        self.store.apply_buy(
            ts=1, target="t", signal_id=None, condition_id="c", token_id="k",
            outcome="Yes", title="m", event_slug="e", shares=10, price=0.5,
            fee=0.0,
        )
        self.store.queue_pending_exit(
            target="t", condition_id="c", token_id="k", outcome="Yes",
            shares=4, reason="target_flat", ts=2,
        )
        self.assertAlmostEqual(
            self.store.pending_exit("t", "c", "k")["shares"], 4)
        # A second target exit is additive but capped at the held shares.
        self.store.queue_pending_exit(
            target="t", condition_id="c", token_id="k", outcome="Yes",
            shares=8, reason="partial_exit", ts=3,
        )
        self.assertAlmostEqual(
            self.store.pending_exit("t", "c", "k")["shares"], 10)

        result = self.store.apply_sell(
            ts=4, target="t", signal_id=None, condition_id="c", token_id="k",
            outcome="Yes", shares=10, price=0.4, fee=0.01, note="strict_exit",
        )
        self.assertIsNotNone(result)
        self.assertIsNone(self.store.pending_exit("t", "c", "k"))
        self.assertEqual(self.store.recent_fills(1)[0]["note"], "strict_exit")

    def test_replace_target_positions(self):
        self.store.add_target_shares("t", "c1", "k1", 100)
        self.store.add_target_shares("t", "c2", "k2", 50)
        updated = self.store.replace_target_positions(
            "t", [("c1", "k1", 70.0), ("c3", "k3", 10.0)], ts=10,
        )
        self.assertEqual(updated, 2)
        self.assertAlmostEqual(self.store.target_shares("t", "k1"), 70.0)
        self.assertEqual(self.store.target_shares("t", "k2"), 0.0)
        self.assertAlmostEqual(self.store.target_shares("t", "k3"), 10.0)

    def test_market_exposure(self):
        self.store.apply_buy(
            ts=1, target="t", signal_id=None, condition_id="c", token_id="k",
            outcome="Yes", title="m", event_slug="e", shares=10, price=0.5,
            fee=0.0,
        )
        self.assertAlmostEqual(self.store.market_exposure("c"), 5.0)

    def test_order_intent_is_idempotent(self):
        first = self.store.create_order(
            dedupe_key="d1", target="t", mode="live", side="BUY",
            condition_id="c", token_id="k", outcome="Yes",
            limit_price=0.5, size=10, fee_rate=0.01, ts=1,
        )
        second = self.store.create_order(
            dedupe_key="d1", target="t", mode="live", side="BUY",
            condition_id="c", token_id="k", outcome="Yes",
            limit_price=0.5, size=10, fee_rate=0.01, ts=2,
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["status"], "INTENT")
        self.store.update_order(first["id"], status="FILLED",
                                filled_size=10, avg_price=0.5, ts=3)
        self.store.commit()
        order = self.store.order_for("d1", "BUY")
        self.assertEqual(order["status"], "FILLED")
        self.assertAlmostEqual(order["filled_size"], 10.0)
        self.assertAlmostEqual(order["fee_rate"], 0.01)

    def test_manual_sell_credit_survives_and_offsets_copy_sell(self):
        self.store.apply_buy(
            ts=1, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", title="m", event_slug="e",
            shares=10, price=0.5, fee=0.0,
        )
        pos = self.store.position("t", "c", "k")
        self.assertAlmostEqual(pos["basis_shares"], 10.0)
        self.assertAlmostEqual(pos["manual_credit"], 0.0)

        # Manual 20%: current shrinks, basis stays.
        self.store.apply_sell(
            ts=2, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", shares=2, price=0.5, fee=0.0,
            manual=True,
        )
        pos = self.store.position("t", "c", "k")
        self.assertAlmostEqual(pos["shares"], 8.0)
        self.assertAlmostEqual(pos["basis_shares"], 10.0)
        self.assertAlmostEqual(pos["manual_credit"], 2.0)
        self.assertEqual(self.store.recent_fills(1)[0]["kind"], "MANUAL")

        # Target sells 50%: equivalent 5 shares, 2 covered by manual credit.
        self.store.apply_sell(
            ts=3, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", shares=3, price=0.5, fee=0.0,
            basis_reduction=5, credit_reduction=2,
        )
        pos = self.store.position("t", "c", "k")
        self.assertAlmostEqual(pos["shares"], 5.0)
        self.assertAlmostEqual(pos["basis_shares"], 5.0)
        self.assertAlmostEqual(pos["manual_credit"], 0.0)

        self.store.apply_sell(
            ts=4, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", shares=5, price=0.5, fee=0.0,
            basis_reduction=5,
        )
        pos = self.store.position("t", "c", "k")
        self.assertEqual(pos["status"], "CLOSED")
        self.assertAlmostEqual(pos["basis_shares"], 0.0)
        self.assertAlmostEqual(pos["manual_credit"], 0.0)

    def test_fill_order_link_and_realized_pnl_since(self):
        self.store.apply_buy(
            ts=10, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", title="m", event_slug="e",
            shares=10, price=0.5, fee=0.0, order_id=7,
        )
        self.store.apply_sell(
            ts=20, target="t", signal_id=None, condition_id="c",
            token_id="k", outcome="Yes", shares=10, price=0.6, fee=0.0,
            order_id=8,
        )
        fills = self.store.recent_fills(2)
        self.assertEqual({f["order_id"] for f in fills}, {7, 8})
        self.assertAlmostEqual(self.store.realized_pnl_since(15), 1.0)


if __name__ == "__main__":
    unittest.main()
