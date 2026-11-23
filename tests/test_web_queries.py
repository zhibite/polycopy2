import os
import tempfile
import unittest
from pathlib import Path

from polycopy.store import Store
from polycopy.web import queries


class WebQueriesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "paper.db"
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.store.ensure_account(1000.0)
        self._old_db = queries.DB_PATH
        queries.DB_PATH = self.db
        self.addCleanup(lambda: setattr(queries, "DB_PATH", self._old_db))

    def _buy(self, ts=1000):
        self.store.apply_buy(
            ts=ts, target="t", signal_id=None, condition_id="c", token_id="k",
            outcome="Yes", title="M", event_slug="e", shares=10, price=0.5,
            fee=0.01,
        )

    def test_summary(self):
        self._buy()
        self.store.set_mark("k", 0.49, 0.51, 0.50, 1000)
        self.store.record_signal(
            target="t", dedupe_key="x", ts=1000, latency_seconds=1.5,
            side="BUY", condition_id="c", token_id="k", outcome="Yes",
            price=0.5, size=10, usd=5.0, title="M", event_slug="e",
            copied=True, reason=None, our_shares=10, our_price=0.5,
            our_notional=5.0, our_fee=0.01,
        )
        self.store.commit()

        conn = queries.connect()
        self.addCleanup(queries.close, conn)
        summary = queries.summary(conn)
        self.assertAlmostEqual(summary["cash"], 1000.0 - 5.01)
        self.assertAlmostEqual(summary["market_value"], 5.0)
        self.assertAlmostEqual(summary["equity"], 999.99)
        self.assertEqual(summary["open_positions"], 1)
        self.assertEqual(summary["signals_total"], 1)
        self.assertEqual(summary["signals_copied"], 1)

    def test_pages_include_position_title(self):
        self._buy()
        self.store.commit()
        conn = queries.connect()
        self.addCleanup(queries.close, conn)

        fills = queries.fills_page(conn, page=1, per_page=50)
        self.assertEqual(fills["total"], 1)
        self.assertEqual(fills["rows"][0]["title"], "M")
        self.assertAlmostEqual(fills["sums"]["buy_notional"], 5.0)

        positions = queries.positions_page(conn, status="open", page=1)
        self.assertEqual(positions["total"], 1)
        self.assertEqual(positions["rows"][0]["title"], "M")
        self.assertEqual(positions["status"], "open")

    def test_signal_summary(self):
        self.store.record_signal(
            target="t", dedupe_key="a", ts=1, latency_seconds=1.0, side="BUY",
            condition_id="c", token_id="k", outcome="Yes", price=0.5, size=10,
            usd=5.0, title="M", event_slug="e", copied=True, reason=None,
        )
        self.store.record_signal(
            target="t", dedupe_key="b", ts=2, latency_seconds=1.0, side="BUY",
            condition_id="c", token_id="k", outcome="Yes", price=0.8, size=10,
            usd=8.0, title="M", event_slug="e", copied=False,
            reason="price_out_of_range",
        )
        self.store.commit()
        conn = queries.connect()
        self.addCleanup(queries.close, conn)
        summary = {r["reason"]: r for r in queries.signal_summary(conn)}
        self.assertEqual(summary[None]["n"], 1)
        self.assertEqual(summary["price_out_of_range"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
