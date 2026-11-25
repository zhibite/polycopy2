#!/usr/bin/env python3
"""Append a portfolio-exposure snapshot for one or more paper DBs.

Reads the SQLite files read-only, so it can run alongside the bots.  One JSON
line per sample, e.g.:

    {"ts": 1795432000, "source": "onchain", "open_positions": 12, ...}

Usage:
    python3 exposure_monitor.py \
      --db data-api=/root/polycopy/data/paper_f705.db \
      --db onchain=/root/polycopy-onchain/data/paper_f705_onchain.db \
      --out /root/polycopy-onchain/data/exposure.jsonl --interval 60
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_DBS = [
    ("onchain", "/root/polycopy/data/paper_f705.db"),
]


def open_ro(path: str) -> sqlite3.Connection | None:
    if not os.path.exists(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        print(f"open {path}: {exc}", file=sys.stderr)
        return None


def snapshot(source: str, path: str) -> dict[str, Any] | None:
    conn = open_ro(path)
    if conn is None:
        return None
    try:
        account = conn.execute(
            "SELECT cash, realized_pnl, fees_paid FROM account WHERE id = 1"
        ).fetchone()
        if account is None:
            return None
        positions = conn.execute(
            "SELECT COUNT(*) AS n,"
            " COALESCE(SUM(shares * avg_cost), 0) AS invested,"
            " COUNT(DISTINCT condition_id) AS conditions"
            " FROM positions WHERE status = 'OPEN'"
        ).fetchone()
        try:
            marked = conn.execute(
                "SELECT COALESCE(SUM(p.shares * COALESCE(m.mid, p.avg_cost)), 0)"
                " AS value FROM positions p LEFT JOIN marks m"
                " ON m.token_id = p.token_id WHERE p.status = 'OPEN'"
            ).fetchone()
            market_value = float(marked["value"]) if marked else 0.0
        except sqlite3.Error:
            market_value = float(positions["invested"])
        top = conn.execute(
            "SELECT condition_id, SUM(shares * avg_cost) AS v FROM positions"
            " WHERE status = 'OPEN' GROUP BY condition_id"
            " ORDER BY v DESC LIMIT 1"
        ).fetchone()
        signal_row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(copied), 0) AS copied"
            " FROM signals"
        ).fetchone()
        cash = float(account["cash"])
        invested = float(positions["invested"])
        equity = cash + market_value
        return {
            "ts": int(time.time()),
            "source": source,
            "db": path,
            "open_positions": int(positions["n"] or 0),
            "open_conditions": int(positions["conditions"] or 0),
            "invested": round(invested, 6),
            "market_value": round(market_value, 6),
            "cash": round(cash, 6),
            "equity": round(equity, 6),
            "realized_pnl": round(float(account["realized_pnl"]), 6),
            "fees_paid": round(float(account["fees_paid"]), 6),
            "unrealized_pnl": round(market_value - invested, 6),
            "cash_utilization": round(invested / equity, 6) if equity > 0 else None,
            "max_market_exposure": round(float(top["v"]), 6) if top else 0.0,
            "max_market_condition": str(top["condition_id"]) if top else None,
            "signals": int(signal_row["n"] or 0),
            "copied_signals": int(signal_row["copied"] or 0),
        }
    except sqlite3.Error as exc:
        print(f"snapshot {path}: {exc}", file=sys.stderr)
        return None
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", action="append", default=None,
                        metavar="LABEL=PATH",
                        help="database to sample (repeatable)")
    parser.add_argument("--out", default="/root/polycopy-onchain/data/exposure.jsonl")
    parser.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args(argv)

    dbs: list[tuple[str, str]] = []
    for item in args.db or [f"{label}={path}" for label, path in DEFAULT_DBS]:
        label, sep, path = item.partition("=")
        if not sep or not label or not path:
            parser.error(f"expected LABEL=PATH, got {item!r}")
        dbs.append((label, path))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    stop = False

    def _stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print(f"exposure monitor: {dbs} -> {out} every {args.interval:.0f}s",
          flush=True)
    with out.open("a", encoding="utf-8") as fh:
        while not stop:
            started = time.time()
            for label, path in dbs:
                row = snapshot(label, path)
                if row is None:
                    print(f"{label}: unavailable ({path})", flush=True)
                    continue
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
                fh.flush()
                print(
                    f"{label}: open={row['open_positions']} "
                    f"inv=${row['invested']:.2f} cash=${row['cash']:.2f} "
                    f"util={row['cash_utilization'] or 0:.1%} "
                    f"max_mkt=${row['max_market_exposure']:.2f}",
                    flush=True,
                )
            elapsed = time.time() - started
            while not stop and time.time() - started < args.interval:
                time.sleep(min(1.0, max(0.1, args.interval - elapsed)))
                elapsed = time.time() - started
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
