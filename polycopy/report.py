"""Console report and CSV export."""
from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Any

from .store import Store


def _money(value: float | None) -> str:
    if value is None:
        return "-"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1_000_000:
        return f"{sign}${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{sign}${value / 1_000:.1f}K"
    return f"{sign}${value:.2f}"


def _pct(value: float) -> str:
    return f"{100 * value:.2f}%"


def build_report(store: Store) -> str:
    acc = store.account()
    positions = store.open_positions(limit=10_000)
    cash = float(acc["cash"])
    starting = float(acc["starting_cash"])
    realized = float(acc["realized_pnl"])
    fees = float(acc["fees_paid"])

    market_value = 0.0
    unrealized = 0.0
    for pos in positions:
        shares = float(pos["shares"])
        avg = float(pos["avg_cost"])
        mark = pos.get("mark_mid")
        mark = float(mark) if mark is not None else avg
        market_value += shares * mark
        unrealized += shares * (mark - avg)
    equity = cash + market_value
    total_pnl = equity - starting

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("POLYCOPY PAPER ACCOUNT")
    lines.append("=" * 78)
    lines.append(f"  starting cash        : {_money(starting)}")
    lines.append(f"  cash                 : {_money(cash)}")
    lines.append(f"  open positions       : {len(positions)}  value {_money(market_value)}")
    lines.append(f"  realized PnL         : {_money(realized)}")
    lines.append(f"  unrealized PnL       : {_money(unrealized)}")
    lines.append(f"  fees paid            : {_money(fees)}")
    lines.append(f"  equity               : {_money(equity)}")
    lines.append(f"  total PnL            : {_money(total_pnl)}  ({_pct(total_pnl / starting) if starting else '-'})")

    total_signals, copied = store.signal_counts()
    lat = store.latency_stats()
    lines.append("")
    lines.append("--- SIGNALS " + "-" * 66)
    lines.append(f"  target trade signals : {total_signals}")
    lines.append(f"  copied               : {copied}")
    if lat:
        lines.append(f"  copy latency (s)     : median {lat['median']:.1f}  p90 {lat['p90']:.1f}  max {lat['max']:.1f}")
    summary = store.signal_summary()
    if summary:
        lines.append("  breakdown:")
        for row in summary:
            label = "COPIED" if row["copied"] else (row["reason"] or "skipped")
            lines.append(f"    {label:<24} {row['n']:>7}   notional {_money(row['notional'])}")

    lines.append("")
    lines.append("--- OPEN POSITIONS " + "-" * 57)
    if not positions:
        lines.append("  none")
    else:
        lines.append(
            f"  {'market':<44} {'out':<4} {'shares':>9} {'avg':>6} {'mark':>6} {'uPnL':>9}"
        )
        for pos in positions[:25]:
            mark = pos.get("mark_mid")
            mark = float(mark) if mark is not None else float(pos["avg_cost"])
            upnl = float(pos["shares"]) * (mark - float(pos["avg_cost"]))
            title = str(pos.get("title") or "")[:44]
            lines.append(
                f"  {title:<44} {str(pos.get('outcome') or '')[:4]:<4} "
                f"{float(pos['shares']):>9.2f} {float(pos['avg_cost']):>6.3f} "
                f"{mark:>6.3f} {upnl:>9.2f}"
            )
        if len(positions) > 25:
            lines.append(f"  ... {len(positions) - 25} more")

    lines.append("")
    lines.append("--- RECENT FILLS " + "-" * 60)
    fills = store.recent_fills(15)
    if not fills:
        lines.append("  none")
    else:
        for fill in fills:
            when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(fill["ts"]))
            lines.append(
                f"  {when}  {fill['side']:<4} {fill['shares']:>8.2f} @ "
                f"{fill['price']:.3f}  {_money(fill['notional']):>8}  "
                f"fee {fill['fee']:.4f}  {str(fill.get('outcome') or '')[:4]}"
            )
    lines.append("")
    return "\n".join(lines)


def export_csv(store: Store, outdir: str | Path) -> list[Path]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, rows in (
        ("positions.csv", store.all_positions()),
        ("fills.csv", store.all_fills()),
        ("signals.csv", store.all_signals()),
    ):
        path = outdir / name
        if rows:
            keys = list(rows[0].keys())
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=keys)
                writer.writeheader()
                writer.writerows(rows)
        else:
            path.write_text("", encoding="utf-8")
        written.append(path)
    return written
