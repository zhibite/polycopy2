import unittest

from polycopy.config import TargetConfig
from polycopy.rules import buy_skip_reason, dedupe_key, desired_notional, target_usd


def row(**overrides):
    base = {
        "type": "TRADE",
        "transaction_hash": "0xabc",
        "condition_id": "c1",
        "token_id": "t1",
        "side": "BUY",
        "price": 0.50,
        "size": 100.0,
        "usdc_size": 50.0,
        "timestamp": 1_000,
        "event_slug": "what-price-will-bitcoin-hit-in-november-2026",
        "slug": "will-bitcoin-reach-80000-in-november-2026",
    }
    base.update(overrides)
    return base


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.t = TargetConfig(address="0xabc")

    def gate(self, r, **kwargs):
        defaults = dict(now=1_010, stale_seconds=300, open_positions=0,
                        has_position=False, market_usd=0.0)
        defaults.update(kwargs)
        return buy_skip_reason(self.t, r, **defaults)

    def test_price_range(self):
        self.assertEqual(self.gate(row(price=0.20)), "price_out_of_range")
        self.assertEqual(self.gate(row(price=0.80)), "price_out_of_range")
        self.assertIsNone(self.gate(row(price=0.30, usdc_size=30.0)))
        self.assertIsNone(self.gate(row(price=0.70, usdc_size=70.0)))

    def test_stale(self):
        self.assertEqual(self.gate(row(), now=2_000), "stale")

    def test_target_too_small(self):
        self.assertEqual(self.gate(row(usdc_size=10.0)), "target_too_small")

    def test_event_exclusion(self):
        self.t.exclude_event_substrings = ["-updown-"]
        self.assertEqual(
            self.gate(row(event_slug="btc-updown-5m-123")), "event_excluded"
        )

    def test_position_limits(self):
        self.assertEqual(self.gate(row(), open_positions=100), "max_open_positions")
        self.assertIsNone(self.gate(row(), open_positions=100, has_position=True))
        self.assertEqual(self.gate(row(), market_usd=300.0), "max_market_exposure")

    def test_target_usd(self):
        self.assertAlmostEqual(target_usd({"size": 10, "price": 0.4}), 4.0)
        self.assertAlmostEqual(
            target_usd({"size": 10, "price": 0.4, "usdc_size": 3.9}), 3.9
        )

    def test_dedupe_key_stable(self):
        self.assertEqual(dedupe_key(row()), dedupe_key(row()))
        self.assertNotEqual(dedupe_key(row()), dedupe_key(row(size=101)))

    def test_sizing(self):
        self.assertAlmostEqual(desired_notional(self.t, 1000.0), 20.0)
        self.assertAlmostEqual(desired_notional(self.t, 1_000_000.0), 100.0)
        self.t.sizing_mode = "fixed"
        self.t.fixed_notional_usd = 7.0
        self.assertAlmostEqual(desired_notional(self.t, 1000.0), 7.0)


if __name__ == "__main__":
    unittest.main()
