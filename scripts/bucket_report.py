#!/usr/bin/env python3
"""Per-price-bucket slippage and concurrent-exposure report.

The 30-70c filter is the copy bucket.  Historical f705 fills in one-sided
markets (1,589 buys, fee-inclusive target VWAP) give these net-of-target-fee
edges per share:

    30-50c  +18.3c   (930 fills, win 56.0%)
    50-60c  +12.1c   (272 fills, win 67.3%)
    60-70c   +6.1c   (387 fills, win 72.1%)

60-70c is thin: at 5x sizing, a 1-2c worse fill materially eats the edge.
This script shows, per bucket, the actual paper slippage (our_price vs the
target's fill price), skip reasons, and the concurrent-exposure time series
written by exposure_monitor.py.

Usage:
    python3 bucket_report.py \
      --db data-api=/root/polycopy/data/paper_f705.db \
      --db onchain=/root/polycopy-onchain/data/paper_f705_onchain.db \
      --exposure /root/polycopy-onchain/data/exposure.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter, defaultdict
from typing import Any

BUCKETS = ("<30c", "30-50c", "50-60c", "60-70c", ">70c")
EDGE_C = {"30-50c": 18.3, "50-60c": 12.1, "60-70c": 6.1}
DEFAULT_DBS = [
    ("onchain", "/root/polycopy/data/paper_f705.db"),
]


def price_bucket(price: float | None) -> str:
    if price is None:
        return "n/a"
    if price < 0.30:
        return "<30c"
    if price < 0.50:
        return "30-50c"
    if price < 0.60:
        return "50-60c"
    if price <= 0.70:
        return "60-70c"
    return ">70c"


def open_ro(path: str) -> sqlite3.Connection | None:
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def load_signals(path: str) -> list[dict[str, Any]] | None:
    conn = open_ro(path)
    if conn is None:
        return None
    try:
        rows = conn.execute("SELECT * FROM signals ORDER BY id").fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return [dict(row) for row in rows]


def current_exposure(path: str) -> dict[str, Any] | None:
    conn = open_ro(path)
    if conn is None:
        return None
    try:
        account = conn.execute(
            "SELECT cash, realized_pnl FROM account WHERE id = 1").fetchone()
        positions = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(shares * avg_cost), 0) AS invested"
            " FROM positions WHERE status = 'OPEN'").fetchone()
        if account is None or positions is None:
            return None
        cash = float(account["cash"])
        invested = float(positions["invested"])
        return {
            "open_positions": int(positions["n"] or 0),
            "invested": invested,
            "cash": cash,
            "equity": cash + invested,
            "realized_pnl": float(account["realized_pnl"]),
            "cash_utilization": invested / (cash + invested)
            if cash + invested > 0 else None,
        }
    finally:
        conn.close()


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def bucket_stats(signals: list[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    for bucket in BUCKETS:
        buys = [s for s in signals
                if str(s.get("side") or "").upper() == "BUY"
                and price_bucket(s.get("price")) == bucket]
        copied = [s for s in buys if s.get("copied") and s.get("our_price") is not None
                  and s.get("our_shares")]
        slips = [100.0 * (float(s["our_price"]) - float(s["price"]))
                 for s in copied if s.get("price") is not None]
        fees = [100.0 * float(s.get("our_fee") or 0) / float(s["our_shares"])
                for s in copied]
        notional = sum(float(s.get("our_notional") or 0) for s in copied)
        reasons = Counter(str(s.get("reason") or "copied")
                          for s in buys if not s.get("copied"))
        slip_median = quantile(slips, 0.5)
        fee_median = quantile(fees, 0.5)
        stats[bucket] = {
            "signals": len(buys),
            "copied": len(copied),
            "skip_reasons": dict(reasons.most_common()),
            "slip_median_c": slip_median,
            "slip_mean_c": sum(slips) / len(slips) if slips else None,
            "slip_p90_c": quantile(slips, 0.9),
            "slip_max_c": max(slips) if slips else None,
            "fee_median_c": fee_median,
            "notional": notional,
            "edge_budget_c": EDGE_C.get(bucket),
            "net_after_median_c": (
                EDGE_C[bucket] - slip_median - (fee_median or 0.0)
                if bucket in EDGE_C and slip_median is not None else None
            ),
        }
    sells = [s for s in signals
             if str(s.get("side") or "").upper() == "SELL"
             and s.get("copied") and s.get("our_price") is not None
             and s.get("price") is not None]
    sell_slips = [100.0 * (float(s["price"]) - float(s["our_price"]))
                  for s in sells]
    stats["_sells"] = {
        "copied": len(sell_slips),
        "slip_median_c": quantile(sell_slips, 0.5),
        "slip_mean_c": sum(sell_slips) / len(sell_slips) if sell_slips else None,
        "slip_p90_c": quantile(sell_slips, 0.9),
    }
    return stats


def exposure_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"samples": 0}
    equity = [float(r.get("equity") or 0) for r in rows]
    peak = equity[0]
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    invested = [float(r.get("invested") or 0) for r in rows]
    util = [float(r.get("cash_utilization") or 0) for r in rows
            if r.get("cash_utilization") is not None]
    return {
        "samples": len(rows),
        "first_ts": rows[0].get("ts"),
        "last_ts": rows[-1].get("ts"),
        "max_open_positions": max(int(r.get("open_positions") or 0) for r in rows),
        "median_open_positions": quantile(
            [float(r.get("open_positions") or 0) for r in rows], 0.5),
        "max_invested": max(invested),
        "median_invested": quantile(invested, 0.5),
        "max_cash_utilization": max(util) if util else None,
        "median_cash_utilization": quantile(util, 0.5),
        "max_market_exposure": max(
            float(r.get("max_market_exposure") or 0) for r in rows),
        "equity_peak": max(equity),
        "equity_trough": min(equity),
        "max_drawdown_pct": 100.0 * max_dd,
        "current": rows[-1],
    }


def fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}{suffix}"


def fmt_c(value: Any) -> str:
    return "-" if value is None else f"{float(value):.2f}c"


def format_report(label: str, path: str, stats: dict[str, Any],
                  exposure: dict[str, Any], notes: list[str]) -> str:
    lines = [
        "=" * 84,
        f"{label}  {path}",
        "=" * 84,
    ]
    lines.extend(notes)
    lines.append(
        f"{'bucket':<9} {'signals':>8} {'copied':>7} {'slip_med':>9} "
        f"{'slip_mean':>10} {'slip_p90':>9} {'slip_max':>9} {'fee_med':>8} "
        f"{'notional':>11} {'edge':>6} {'net_med':>8}  flag"
    )
    for bucket in BUCKETS:
        row = stats[bucket]
        flag = ""
        if bucket in EDGE_C:
            med = row["slip_median_c"]
            p90 = row["slip_p90_c"]
            net = row["net_after_median_c"]
            if not row["copied"]:
                flag = "no copies yet"
            elif bucket == "60-70c":
                # Thin edge: 2c median / 4c p90 slippage is material.
                if (med is not None and med > 2.0) or \
                   (p90 is not None and p90 > 4.0) or \
                   (net is not None and net <= 0):
                    flag = "WARNING thin-edge"
                else:
                    flag = "ok"
            else:
                # Thicker-edge buckets: only flag when the edge is eroded.
                if net is not None and net <= 0:
                    flag = "WARNING edge-eroded"
                elif med is not None and med > 0.5 * EDGE_C[bucket]:
                    flag = "WARNING slip>50% edge"
                else:
                    flag = "ok"
        lines.append(
            f"{bucket:<9} {row['signals']:>8} {row['copied']:>7} "
            f"{fmt(row['slip_median_c']):>9} {fmt(row['slip_mean_c']):>10} "
            f"{fmt(row['slip_p90_c']):>9} {fmt(row['slip_max_c']):>9} "
            f"{fmt(row['fee_median_c']):>8} {row['notional']:>11.2f} "
            f"{fmt(row['edge_budget_c'], 1):>6} "
            f"{fmt(row['net_after_median_c'], 2):>8}  {flag}"
        )
    lines.append("")
    lines.append("slippage is cents/share; positive = worse than the target fill")
    lines.append("edge = historical fee-inclusive target edge (30-70c sample); "
                 "net_med subtracts our median slip and median fee")
    for bucket in ("30-50c", "50-60c", "60-70c"):
        reasons = stats[bucket]["skip_reasons"]
        if reasons:
            lines.append(f"{bucket} skip reasons: {reasons}")
    sells = stats["_sells"]
    lines.append(
        f"SELL copied={sells['copied']}  median slip="
        f"{fmt_c(sells['slip_median_c'])}  mean={fmt_c(sells['slip_mean_c'])}  "
        f"p90={fmt_c(sells['slip_p90_c'])}"
    )
    lines.append("")
    if exposure.get("samples"):
        lines.append(
            f"concurrent exposure: samples={exposure['samples']}  "
            f"max_open={exposure['max_open_positions']} "
            f"median_open={fmt(exposure['median_open_positions'], 0)}  "
            f"max_invested=${exposure['max_invested']:.2f} "
            f"median_invested=${fmt(exposure['median_invested'])
                               if exposure['median_invested'] is not None else '-'}  "
            f"max_util={fmt(None if exposure['max_cash_utilization'] is None
                            else 100 * exposure['max_cash_utilization'], 1, '%')}  "
            f"max_market=${exposure['max_market_exposure']:.2f}  "
            f"max_dd={fmt(exposure['max_drawdown_pct'], 2, '%')}"
        )
        current = exposure["current"]
        lines.append(
            f"  current: open={current.get('open_positions')} "
            f"cash=${fmt(current.get('cash'))} "
            f"invested=${fmt(current.get('invested'))} "
            f"equity=${fmt(current.get('equity'))} "
            f"realized=${fmt(current.get('realized_pnl'))}"
        )
    elif exposure.get("current"):
        current = exposure["current"]
        lines.append(
            f"concurrent exposure: no monitor samples yet; current "
            f"open={current['open_positions']} invested=${current['invested']:.2f} "
            f"cash=${current['cash']:.2f} "
            f"util={fmt(None if current['cash_utilization'] is None
                        else 100 * current['cash_utilization'], 1, '%')}"
        )
    else:
        lines.append("concurrent exposure: no data")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", action="append", default=None,
                        metavar="LABEL=PATH", help="database (repeatable)")
    parser.add_argument("--exposure",
                        default="/root/polycopy-onchain/data/exposure.jsonl")
    parser.add_argument("--since", type=int, default=None,
                        help="only signals/exposure at/after this epoch")
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    dbs: list[tuple[str, str]] = []
    for item in args.db or [f"{label}={path}" for label, path in DEFAULT_DBS]:
        label, sep, path = item.partition("=")
        if not sep or not label or not path:
            parser.error(f"expected LABEL=PATH, got {item!r}")
        dbs.append((label, path))

    exposure: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if os.path.exists(args.exposure):
        with open(args.exposure, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                exposure[str(row.get("source") or "")].append(row)

    report: dict[str, Any] = {}
    text: list[str] = []
    for label, path in dbs:
        signals = load_signals(path)
        if signals is None:
            text.append(f"=== {label}: no database at {path} ===")
            continue
        if args.since:
            signals = [s for s in signals
                       if int(s.get("ts") or 0) >= args.since]
        stats = bucket_stats(signals)
        rows = exposure.get(label, [])
        if args.since:
            rows = [r for r in rows
                    if int(r.get("ts") or 0) >= args.since]
        exp = exposure_stats(rows) if rows else {
            "samples": 0, "current": current_exposure(path)}
        notes = [
            f"signals={len(signals)} copied="
            f"{sum(1 for s in signals if s.get('copied'))}",
        ]
        if args.since:
            notes.append(f"since={args.since}")
        text.append(format_report(label, path, stats, exp, notes))
        report[label] = {"path": path, "stats": stats, "exposure": exp}
        text.append("")

    print("\n".join(text).rstrip())
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
