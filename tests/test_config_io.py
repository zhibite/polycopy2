import tempfile
import unittest
from pathlib import Path

from polycopy.config import AccountConfig, AppConfig, PaperConfig, TargetConfig, load_config
from polycopy.web import config_io


class ConfigIOTest(unittest.TestCase):
    def test_roundtrip(self):
        cfg = AppConfig(
            account=AccountConfig(starting_cash=12345.0),
            paper=PaperConfig(poll_seconds=7, db_path="data/x.db", log_path="data/x.log"),
            targets={
                "t": TargetConfig(
                    address="0xabc",
                    buy_price_min=0.2,
                    buy_price_max=0.8,
                    scale_factor=0.03,
                    fee_rate=0.05,
                    exclude_event_substrings=["-updown-"],
                )
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            config_io.save_config(cfg, path)
            loaded = load_config(path)
            self.assertEqual(loaded.paper.poll_seconds, 7)
            self.assertEqual(loaded.targets["t"].buy_price_max, 0.8)
            self.assertEqual(loaded.targets["t"].scale_factor, 0.03)
            self.assertEqual(loaded.targets["t"].fee_rate, 0.05)
            self.assertEqual(loaded.targets["t"].exclude_event_substrings, ["-updown-"])

            # Second save creates a backup of the previous file.
            cfg.paper.poll_seconds = 9
            config_io.save_config(cfg, path)
            self.assertTrue(path.with_name("config.toml.bak").exists())
            self.assertEqual(load_config(path).paper.poll_seconds, 9)

    def test_none_fee_rate_is_omitted(self):
        cfg = AppConfig(
            account=AccountConfig(),
            paper=PaperConfig(),
            targets={"t": TargetConfig(address="0xabc", fee_rate=None)},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            config_io.save_config(cfg, path)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("fee_rate = None", text)
            self.assertIsNone(load_config(path).targets["t"].fee_rate)


if __name__ == "__main__":
    unittest.main()
