"""Command line entry point."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .api import ClobApi, DataApi, GammaApi
from .config import AppConfig, load_config
from .engine import Engine
from .http import HttpClient
from .onchain import OnchainSettings, RpcClient
from .onchain_runner import build_and_run
from .onchain_verify import format_verification, verify_wallet
from .report import build_report, export_csv
from .store import Store


def setup_logging(cfg: AppConfig, log_path: str | None = None) -> logging.Logger:
    logger = logging.getLogger("polycopy")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    fmt.converter = time.gmtime

    path = Path(log_path or cfg.paper.log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
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


def _build(cfg: AppConfig, db_path: str | None = None,
           log_path: str | None = None):
    setup_logging(cfg, log_path=log_path)
    store = Store(db_path or cfg.paper.db_path)
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


def _build_http(cfg: AppConfig) -> HttpClient:
    return HttpClient(
        timeout=cfg.paper.request_timeout,
        retries=cfg.paper.request_retries,
        logger=logging.getLogger("polycopy.http"),
    )


def _onchain_settings(args: argparse.Namespace) -> OnchainSettings:
    settings = OnchainSettings.from_env()
    rpc = getattr(args, "rpc", None)
    ws = getattr(args, "ws", None)
    if rpc:
        settings.rpc_urls = list(rpc)
    if ws:
        settings.ws_urls = list(ws)
    if getattr(args, "no_ws", False):
        settings.ws_urls = []
    if getattr(args, "lookback", None) is not None:
        settings.lookback_seconds = int(args.lookback)
    if getattr(args, "catchup_chunk", None) is not None:
        settings.catchup_chunk = int(args.catchup_chunk)
    if getattr(args, "trace", None):
        settings.trace_path = args.trace
    return settings


def _cmd_verify(cfg: AppConfig, args: argparse.Namespace) -> int:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            stream=sys.stderr,
        )
    settings = _onchain_settings(args)
    wallet = args.wallet or next(
        (target.address for target in cfg.targets.values() if target.enabled),
        None,
    )
    if not wallet:
        print("no wallet given and no enabled target in the config",
              file=sys.stderr)
        return 2
    result = verify_wallet(
        _build_http(cfg), RpcClient(settings.rpc_urls), wallet,
        count=args.count, lookback=args.lookback,
    )
    print(format_verification(result))
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nwrote {path}")
    if result["matched"] == 0:
        return 1
    return 0 if result["failed"] == 0 else 1


def _cmd_onchain_run(cfg: AppConfig, args: argparse.Namespace) -> int:
    store, engine = _build(cfg, db_path=args.db, log_path=args.log)
    settings = _onchain_settings(args)
    try:
        return build_and_run(
            engine, settings, logger=logging.getLogger("polycopy.onchain"),
        )
    finally:
        store.commit()


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
        if name in ("init", "report", "settle", "mark"):
            p.add_argument("--db", default=None,
                           help="override the database path from the config")
        if name == "run":
            p.add_argument("--once", action="store_true", help="run a single cycle")
            p.add_argument("--cycles", type=int, default=None,
                           help="stop after N cycles")
        if name == "report":
            p.add_argument("--csv", default=None,
                           help="also export positions/fills/signals CSV to this dir")

    onchain = sub.add_parser(
        "onchain", help="decode OrderFilled logs and trade from RPC"
    )
    onchain_sub = onchain.add_subparsers(dest="onchain_command", required=True)

    verify = onchain_sub.add_parser(
        "verify", help="compare decoded OrderFilled events with data-api trades"
    )
    verify.add_argument("--config", default="configs/paper_f705.toml")
    verify.add_argument("--wallet", default=None,
                        help="wallet to check (default: first enabled target)")
    verify.add_argument("--count", type=int, default=30,
                        help="number of recent data-api trades to check")
    verify.add_argument("--lookback", type=int, default=86_400,
                        help="data-api lookback window in seconds")
    verify.add_argument("--rpc", action="append", default=None,
                        help="HTTP RPC URL (repeatable)")
    verify.add_argument("--json-out", default=None,
                        help="write the full result as JSON to this path")

    orun = onchain_sub.add_parser(
        "run", help="paper trade from RPC OrderFilled logs"
    )
    orun.add_argument("--config", default="configs/paper_f705.toml")
    orun.add_argument("--db", default=None,
                      help="separate paper DB (recommended for parallel runs)")
    orun.add_argument("--log", default=None, help="override the log path")
    orun.add_argument("--trace", default=None,
                      help="append per-fill latency JSON lines to this path")
    orun.add_argument("--rpc", action="append", default=None,
                      help="HTTP RPC URL (repeatable)")
    orun.add_argument("--ws", action="append", default=None,
                      help="WebSocket RPC URL (repeatable)")
    orun.add_argument("--no-ws", action="store_true",
                      help="use HTTP log polling instead of a subscription")
    orun.add_argument("--lookback", type=int, default=None,
                      help="initial catch-up window in seconds at first start")
    orun.add_argument("--catchup-chunk", type=int, default=None,
                      help="blocks per eth_getLogs catch-up call")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "onchain":
        if args.onchain_command == "verify":
            return _cmd_verify(cfg, args)
        if args.onchain_command == "run":
            return _cmd_onchain_run(cfg, args)
        return 1

    if args.command == "init":
        store = Store(args.db or cfg.paper.db_path)
        store.ensure_account(cfg.account.starting_cash)
        print(f"initialised {store.path} with "
              f"${cfg.account.starting_cash:,.2f} paper cash")
        return 0

    if args.command == "report":
        store = Store(args.db or cfg.paper.db_path)
        store.ensure_account(cfg.account.starting_cash)
        print(build_report(store))
        if args.csv:
            paths = export_csv(store, args.csv)
            print("exported: " + ", ".join(str(p) for p in paths))
        return 0

    store, engine = _build(cfg, db_path=getattr(args, "db", None))
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
            cfg.paper.poll_seconds, store.path,
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
