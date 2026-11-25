import tempfile
import unittest
from pathlib import Path

from polycopy.config import (
    AccountConfig, AppConfig, LiveConfig, PaperConfig, RunConfig, TargetConfig,
    load_config,
)
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

    def test_unknown_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[account]\nstarting_cash = 1000.0\n\n"
                "[paper]\npoll_seconds = 5\nnew_option = 1\n\n"
                "[targets.t]\naddress = \"0xabc\"\nfuture_flag = true\n",
                encoding="utf-8",
            )
            cfg = load_config(path)
            self.assertEqual(cfg.paper.poll_seconds, 5)
            self.assertEqual(cfg.targets["t"].address, "0xabc")

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

    def test_run_and_live_roundtrip(self):
        cfg = AppConfig(
            account=AccountConfig(),
            paper=PaperConfig(),
            run=RunConfig(mode="live"),
            live=LiveConfig(
                dry_run=False,
                funder="0xfunder",
                private_key_env="TEST_POLY_KEY",
                signature_type=2,
                max_order_usd=25.0,
                max_daily_loss_usd=10.0,
                kill_switch_path="data/TEST_HALT",
            ),
            targets={"t": TargetConfig(address="0xabc")},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            config_io.save_config(cfg, path)
            loaded = load_config(path)
            self.assertEqual(loaded.run.mode, "live")
            self.assertFalse(loaded.live.dry_run)
            self.assertEqual(loaded.live.funder, "0xfunder")
            self.assertEqual(loaded.live.private_key_env, "TEST_POLY_KEY")
            self.assertEqual(loaded.live.max_order_usd, 25.0)
            self.assertEqual(loaded.live.kill_switch_path, "data/TEST_HALT")

    def test_live_signature_type_3_is_valid(self):
        live = LiveConfig(signature_type=3)
        live.validate()
        self.assertEqual(live.signature_type, 3)
        with self.assertRaises(ValueError):
            LiveConfig(signature_type=4).validate()

    def test_missing_run_section_defaults_to_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[account]\nstarting_cash = 1000.0\n\n"
                "[targets.t]\naddress = \"0xabc\"\n",
                encoding="utf-8",
            )
            cfg = load_config(path)
            self.assertEqual(cfg.run.mode, "paper")
            self.assertTrue(cfg.live.dry_run)


if __name__ == "__main__":
    unittest.main()
