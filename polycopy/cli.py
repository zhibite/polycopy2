"""Command line entry point."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .api import ClobApi, DataApi, GammaApi
from .config import AppConfig, load_config
from .engine import Engine
from .http import HttpClient
from .report import build_report, export_csv
from .store import Store


def setup_logging(cfg: AppConfig) -> logging.Logger:
    logger = logging.getLogger("polycopy")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    fmt.converter = time.gmtime

    log_path = Path(cfg.paper.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console)
    logger.propagate = False
    return logger


def _build(cfg: AppConfig):
    logger = setup_logging(cfg)
    store = Store(cfg.paper.db_path)
    store.ensure_account(cfg.account.starting_cash)
    http = HttpClient(
        timeout=cfg.paper.request_timeout,
        retries=cfg.paper.request_retries,
        logger=logging.getLogger("polycopy.http"),
    )
    return store, Engine(
        cfg, store, DataApi(http), ClobApi(http), GammaApi(http),
        logger=logging.getLogger("polycopy.engine"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="polycopy", description="Polymarket paper copy-trading system"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("init", "create the database and paper account"),
        ("run", "poll targets and simulate copy trades"),
        ("report", "print account, signals and positions"),
        ("settle", "run one settlement pass for resolved markets"),
        ("mark", "refresh marks for open positions"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--config", default="configs/paper_f705.toml")
        if name == "run":
            p.add_argument("--once", action="store_true", help="run a single cycle")
            p.add_argument("--cycles", type=int, default=None,
                           help="stop after N cycles")
        if name == "report":
            p.add_argument("--csv", default=None,
                           help="also export positions/fills/signals CSV to this dir")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "init":
        store = Store(cfg.paper.db_path)
        store.ensure_account(cfg.account.starting_cash)
        print(f"initialised {cfg.paper.db_path} with "
              f"${cfg.account.starting_cash:,.2f} paper cash")
        return 0

    if args.command == "report":
        store = Store(cfg.paper.db_path)
        store.ensure_account(cfg.account.starting_cash)
        print(build_report(store))
        if args.csv:
            paths = export_csv(store, args.csv)
            print("exported: " + ", ".join(str(p) for p in paths))
        return 0

    store, engine = _build(cfg)
    if args.command == "settle":
        count = engine.settle()
        print(f"settled {count} positions")
        return 0
    if args.command == "mark":
        count = engine.mark()
        print(f"marked {count} positions")
        return 0
    if args.command == "run":
        cycles = 1 if args.once else args.cycles
        engine.log.info(
            "starting paper engine: %d target(s), poll=%ds db=%s",
            len([t for t in cfg.targets.values() if t.enabled]),
            cfg.paper.poll_seconds, cfg.paper.db_path,
        )
        for name, target in cfg.targets.items():
            engine.log.info(
                "target %s %s enabled=%s sizing=%s scale=%s price=[%.2f, %.2f]",
                name, target.address, target.enabled, target.sizing_mode,
                target.scale_factor, target.buy_price_min, target.buy_price_max,
            )
        try:
            engine.run(cycles=cycles)
        except KeyboardInterrupt:
            engine.log.info("stopped by user")
        finally:
            store.commit()
        return 0
    return 1
