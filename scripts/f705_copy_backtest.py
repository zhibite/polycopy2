#!/usr/bin/env python3
"""Event-driven backtest of copying f705 from a fetch_wallet probe directory.

Mirrors the paper engine's sizing and gates:
  * BUY only when the target fill price is inside [min_price, max_price]
  * notional = target cash * scale, capped by max_notional and per-market cap
  * cash reserve, min notional, min shares, max open positions
  * copy SELLs as a fraction of the target's position
  * settle on target REDEEM / MERGE / SPLIT and on resolved closed positions
  * mark open positions at the last target trade price (or the probe's
    current_price for positions still open at the end)

Usage:
  python3 -I scripts/f705_copy_backtest.py /root/polycopy/probe_f705 \
      --scale 0.05 --fee 0.01 --slip 0.01 --cash 10000
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone


def load_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def fnum(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt(value: float) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1e6:
        return f"{sign}${value / 1e6:,.2f}M"
    if value >= 1e3:
        return f"{sign}${value / 1e3:,.1f}K"
    return f"{sign}${value:,.2f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("probe")
    ap.add_argument("--cash", type=float, default=10_000.0)
    ap.add_argument("--scale", type=float, default=0.05)
    ap.add_argument("--fee", type=float, default=0.01)
    ap.add_argument("--slip", type=float, default=0.01,
                    help="adverse price per share on both buys and sells")
    ap.add_argument("--min-price", type=float, default=0.30)
    ap.add_argument("--max-price", type=float, default=0.70)
    ap.add_argument("--min-target", type=float, default=20.0)
    ap.add_argument("--max-notional", type=float, default=500.0)
    ap.add_argument("--min-notional", type=float, default=2.0)
    ap.add_argument("--max-market", type=float, default=500.0)
    ap.add_argument("--max-open", type=int, default=300)
    ap.add_argument("--reserve", type=float, default=0.05)
    ap.add_argument("--min-shares", type=float, default=1.0)
    ap.add_argument("--days", type=int, default=0,
                    help="limit the drawdown window to the last N days; 0 = all")
    ap.add_argument("--no-synthetic-settle", action="store_true")
    ap.add_argument("--curve-out", default=None)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    probe = args.probe
    activity = load_jsonl(os.path.join(probe, "activity.jsonl"))
    closed = load_jsonl(os.path.join(probe, "positions_closed.jsonl"))
    open_pos = load_jsonl(os.path.join(probe, "positions_open.jsonl"))

    activity.sort(key=lambda r: int(fnum(r.get("timestamp"))))

    # ------------------------------------------------------------- metadata
    cond_tokens: dict[str, set[str]] = defaultdict(set)
    for row in activity + closed + open_pos:
        cond = str(row.get("condition_id") or "")
        token = str(row.get("token_id") or "")
        if cond and token:
            cond_tokens[cond].add(token)
        opposite = str(row.get("opposite_token_id") or "")
        if cond and opposite:
            cond_tokens[cond].add(opposite)

    open_price: dict[str, float] = {}
    for row in open_pos:
        token = str(row.get("token_id") or "")
        if token:
            open_price[token] = fnum(row.get("current_price"), 0.0)

    # Resolved positions whose market never produced a REDEEM row for us.
    # A CLOSED position priced at 0 or 1 is a resolution; use its last event
    # time so a loser does not stay marked at an old trade price forever.
    settle_by_token: dict[str, tuple[int, float, str]] = {}
    if not args.no_synthetic_settle:
        for row in closed:
            if str(row.get("status") or "").upper() != "CLOSED":
                continue
            if row.get("mergeable"):
                continue
            price = row.get("current_price")
            if price is None:
                continue
            price = fnum(price, None)
            if price is None:
                continue
            if price <= 0.001:
                payout = 0.0
            elif price >= 0.999:
                payout = 1.0
            else:
                continue
            token = str(row.get("token_id") or "")
            ts = int(fnum(row.get("last_event_at")))
            cond = str(row.get("condition_id") or "")
            if not token or ts <= 0:
                continue
            prev = settle_by_token.get(token)
            if prev is None or ts >= prev[0]:
                settle_by_token[token] = (ts, payout, cond)

    # ----------------------------------------------------------------- events
    events: list[tuple[int, int, int, dict]] = []
    for idx, row in enumerate(activity):
        events.append((int(fnum(row.get("timestamp"))), 0, idx, row))
    for idx, (token, (ts, payout, cond)) in enumerate(settle_by_token.items()):
        events.append((ts, 1, idx, {
            "type": "SETTLE", "token_id": token, "condition_id": cond,
            "payout": payout,
        }))
    events.sort(key=lambda item: (item[0], item[1], item[2]))

    # ------------------------------------------------------------------ state
    cash = float(args.cash)
    start_equity = cash
    target_shares: dict[str, float] = defaultdict(float)
    our: dict[str, dict] = {}
    last_price: dict[str, float] = {}
    realized = 0.0
    fees_paid = 0.0
    processed = Counter()
    skipped = Counter()
    curve: list[tuple[int, float, float, float]] = []
    peak = start_equity
    peak_ts = events[0][0] if events else 0
    max_dd = 0.0
    dd_info: tuple[float, int, int] | None = None
    max_invested = 0.0
    max_open = 0

    def market_exposure(cond: str) -> float:
        return sum(p["shares"] * p["avg_cost"] for p in our.values()
                   if p["cond"] == cond and p["shares"] > 1e-12)

    def open_count() -> int:
        return sum(1 for p in our.values() if p["shares"] > 1e-12)

    def mark(token: str) -> float:
        price = last_price.get(token)
        if price is None or price <= 0:
            position = our.get(token)
            return position["avg_cost"] if position else 0.0
        return price

    def equity() -> tuple[float, float]:
        invested = sum(p["shares"] * mark(t) for t, p in our.items()
                       if p["shares"] > 1e-12)
        return cash + invested, invested

    def record(ts: int) -> None:
        nonlocal peak, peak_ts, max_dd, dd_info, max_invested, max_open
        eq, invested = equity()
        curve.append((ts, eq, cash, invested))
        max_invested = max(max_invested, invested)
        max_open = max(max_open, open_count())
        if eq > peak:
            peak = eq
            peak_ts = ts
        drawdown = peak - eq
        if drawdown > max_dd:
            max_dd = drawdown
            dd_info = (peak, peak_ts, ts)

    def settle_position(token: str, payout: float, ts: int) -> None:
        nonlocal cash, realized
        position = our.pop(token, None)
        if position is None or position["shares"] <= 1e-12:
            return
        shares = position["shares"]
        proceeds = shares * payout
        pnl = proceeds - shares * position["avg_cost"]
        cash += proceeds
        realized += pnl

    def settle_condition(cond: str, token: str, payout: float, ts: int) -> None:
        """Settle one outcome; at 0/1 also settle the opposite token."""
        if not cond:
            if token in our:
                settle_position(token, payout, ts)
            return
        if payout <= 0.001 or payout >= 0.999:
            for held_token, position in list(our.items()):
                if position["cond"] != cond:
                    continue
                outcome = payout if held_token == token else 1.0 - payout
                settle_position(held_token, outcome, ts)
        elif token in our:
            settle_position(token, payout, ts)

    def execute_buy(row: dict) -> None:
        nonlocal cash, fees_paid
        token = str(row.get("token_id") or "")
        cond = str(row.get("condition_id") or "")
        price = fnum(row.get("price"))
        size = fnum(row.get("size"))
        if not token or not cond or price <= 0 or price > 1 or size <= 0:
            return
        usd = row.get("usdc_size")
        usd = fnum(usd) if usd is not None else size * price
        processed["target_buys"] += 1
        if not (args.min_price <= price <= args.max_price):
            skipped["price_out_of_range"] += 1
            return
        if usd < args.min_target:
            skipped["target_too_small"] += 1
            return
        has_position = token in our
        if not has_position and open_count() >= args.max_open:
            skipped["max_open_positions"] += 1
            return
        market_usd = market_exposure(cond)
        available = max(0.0, cash * (1.0 - args.reserve))
        remaining_market = max(0.0, args.max_market - market_usd)
        desired = min(usd * args.scale, args.max_notional)
        budget = min(desired, remaining_market, available)
        if budget < args.min_notional:
            if available < args.min_notional:
                skipped["insufficient_cash"] += 1
                return
            if remaining_market < args.min_notional:
                skipped["max_market_exposure"] += 1
                return
            budget = args.min_notional
        shares = budget / price
        if shares < args.min_shares:
            need = args.min_shares * price
            if (need > args.max_notional or need > remaining_market
                    or need > available):
                skipped["below_min_size"] += 1
                return
            shares = args.min_shares
            budget = need

        fill = price + args.slip
        cap = min(price + 0.03, max(price * 1.10, price + 0.001))
        if fill > cap + 1e-9:
            skipped["drift"] += 1
            return
        notional = shares * fill
        fee = notional * (1.0 - fill) * args.fee
        if notional > remaining_market + 1e-9 or notional + fee > available + 1e-9:
            scale = 1.0
            if notional > 0:
                scale = min(scale, remaining_market / notional)
            if notional + fee > 0:
                scale = min(scale, available / (notional + fee))
            shares *= max(0.0, scale)
            if shares * price < args.min_notional:
                skipped["insufficient_cash"] += 1
                return
            notional = shares * fill
            fee = notional * (1.0 - fill) * args.fee
        if shares <= 0 or notional <= 0:
            return
        total_cost = notional + fee
        cash -= total_cost
        fees_paid += fee
        position = our.get(token)
        if position is not None and position["shares"] > 1e-12:
            old_shares = position["shares"]
            new_shares = old_shares + shares
            position["avg_cost"] = (
                position["avg_cost"] * old_shares + total_cost
            ) / new_shares
            position["shares"] = new_shares
        else:
            our[token] = {
                "shares": shares,
                "avg_cost": total_cost / shares,
                "cond": cond,
            }
        processed["copied_buys"] += 1
        processed["copied_notional"] += notional

    def execute_sell(row: dict) -> None:
        nonlocal cash, realized, fees_paid
        token = str(row.get("token_id") or "")
        price = fnum(row.get("price"))
        size = fnum(row.get("size"))
        if not token or price <= 0 or size <= 0:
            return
        before = target_shares.get(token, 0.0)
        frac = min(1.0, size / before) if before > 1e-12 else 0.0
        position = our.get(token)
        if position is None or position["shares"] <= 1e-12 or frac <= 0:
            return
        shares = position["shares"] * frac
        if shares <= 1e-12:
            return
        fill = max(0.001, price - args.slip)
        proceeds = shares * fill
        fee = proceeds * (1.0 - fill) * args.fee
        pnl = proceeds - fee - shares * position["avg_cost"]
        cash += proceeds - fee
        realized += pnl
        fees_paid += fee
        position["shares"] -= shares
        if position["shares"] <= 1e-12:
            del our[token]
        processed["copied_sells"] += 1

    # --------------------------------------------------------------- main loop
    for ts, _kind, _idx, row in events:
        kind = str(row.get("type") or "").upper()
        if kind == "TRADE":
            token = str(row.get("token_id") or "")
            price = fnum(row.get("price"))
            size = fnum(row.get("size"))
            side = str(row.get("side") or "").upper()
            if token and price > 0:
                last_price[token] = price
            if side == "BUY":
                if token:
                    target_shares[token] += size
                execute_buy(row)
            elif side == "SELL":
                execute_sell(row)
                if token:
                    target_shares[token] = max(
                        0.0, target_shares.get(token, 0.0) - size)
            processed["trades"] += 1
        elif kind == "REDEEM":
            token = str(row.get("token_id") or "")
            cond = str(row.get("condition_id") or "")
            size = fnum(row.get("size"))
            usd = fnum(row.get("usdc_size"))
            if token:
                target_shares[token] = 0.0
            if token and size > 0:
                payout = min(1.0, max(0.0, usd / size))
                settle_condition(cond, token, payout, ts)
                processed["redeems"] += 1
        elif kind == "MERGE":
            cond = str(row.get("condition_id") or "")
            size = fnum(row.get("size"))
            for token in cond_tokens.get(cond, ()):
                target_shares[token] = max(
                    0.0, target_shares.get(token, 0.0) - size)
            processed["merges"] += 1
        elif kind == "SPLIT":
            cond = str(row.get("condition_id") or "")
            size = fnum(row.get("size"))
            for token in cond_tokens.get(cond, ()):
                target_shares[token] += size
            processed["splits"] += 1
        elif kind == "SETTLE":
            token = str(row.get("token_id") or "")
            cond = str(row.get("condition_id") or "")
            payout = fnum(row.get("payout"))
            settle_condition(cond, token, payout, ts)
            processed["settles"] += 1
        record(ts)

    # Final marks: use the probe's current price for tokens still open.
    for token in list(our):
        if token in open_price and open_price[token] > 0:
            last_price[token] = open_price[token]
    if events:
        record(events[-1][0])

    # --------------------------------------------------------------- metrics
    final_equity, final_invested = equity()
    unrealized = sum(
        p["shares"] * (mark(t) - p["avg_cost"])
        for t, p in our.items() if p["shares"] > 1e-12
    )

    def drawdown(points: list[tuple[int, float, float, float]]):
        if not points:
            return 0.0, 0.0, None, None, None
        peak_value = points[0][1]
        peak_at = points[0][0]
        worst = 0.0
        worst_info = None
        for ts, eq, _cash, _inv in points:
            if eq > peak_value:
                peak_value = eq
                peak_at = ts
            drop = peak_value - eq
            if drop > worst:
                worst = drop
                worst_info = (peak_value, peak_at, ts)
        # Percentage must use the peak that preceded this trough, not a later
        # (higher) peak reached after the recovery.
        pct = worst / worst_info[0] if worst_info and worst_info[0] else 0.0
        return worst, pct, worst_info, peak_value, peak_at

    windows = {}
    last_ts = events[-1][0] if events else 0
    for label, days in (("all", 0), ("90d", 90), ("30d", 30)):
        if days:
            cutoff = last_ts - days * 86400
            points = [p for p in curve if p[0] >= cutoff]
            if points:
                # Start the window at the equity carried into it.
                prior = [p for p in curve if p[0] < cutoff]
                start_point = (cutoff, prior[-1][1] if prior else start_equity,
                               prior[-1][2] if prior else start_equity, 0.0)
                points = [start_point] + points
        else:
            points = [(curve[0][0] - 1, start_equity, start_equity, 0.0)] + curve
        dd_usd, dd_pct, info, _peak, _peak_ts = drawdown(points)
        windows[label] = {
            "max_drawdown_usd": round(dd_usd, 2),
            "max_drawdown_pct": round(100.0 * dd_pct, 4),
            "peak_equity": round(info[0], 2) if info else None,
            "peak_ts": info[1] if info else None,
            "trough_ts": info[2] if info else None,
            "points": len(points),
        }

    # Daily last-point drawdown, a less noisy view.
    daily: dict[str, tuple[int, float, float, float]] = {}
    for point in curve:
        day = datetime.fromtimestamp(
            point[0], timezone.utc).strftime("%Y-%m-%d")
        daily[day] = point
    daily_points = [daily[d] for d in sorted(daily)]
    daily_dd, daily_dd_pct, daily_info, _dp, _dts = drawdown(daily_points)

    # A new account starts with `--cash` at any point in history.  Position
    # sizing is proportional to the target's fill, not to our equity, so the
    # daily dollar PnL sequence can be replayed from a fresh starting balance.
    # This answers "if I started with $10k on date X, what was the worst
    # drawdown over the next 30/90 days?" without compounding away the risk.
    daily_pnl = [daily_points[i][1] - daily_points[i - 1][1]
                 for i in range(1, len(daily_points))]
    fresh: dict[str, dict] = {}
    for horizon in (30, 90, 180, 365):
        values: list[float] = []
        worst = 0.0
        worst_window: tuple[int, int] | None = None
        worst_end = 0.0
        for start in range(0, len(daily_pnl) - horizon):
            eq = start_equity
            peak = eq
            worst_here = 0.0
            for offset in range(start + 1, start + horizon + 1):
                eq += daily_pnl[offset - 1]
                if eq > peak:
                    peak = eq
                if peak - eq > worst_here:
                    worst_here = peak - eq
            values.append(worst_here / start_equity if start_equity else 0.0)
            if worst_here > worst:
                worst = worst_here
                worst_window = (start, start + horizon)
                worst_end = eq
        if not values:
            continue
        values.sort()

        def q(p: float) -> float:
            return values[min(len(values) - 1,
                              max(0, int(round(p * (len(values) - 1)))))]

        fresh[str(horizon)] = {
            "windows": len(values),
            "median_pct": round(100.0 * q(0.5), 2),
            "p90_pct": round(100.0 * q(0.9), 2),
            "max_pct": round(100.0 * values[-1], 2),
            "max_usd": round(worst, 2),
            "max_window_start": (daily_points[worst_window[0]][0]
                                 if worst_window else None),
            "max_window_end": (daily_points[worst_window[1]][0]
                               if worst_window else None),
            "max_window_end_equity": round(worst_end, 2),
        }

    result = {
        "probe": probe,
        "params": {
            "cash": args.cash, "scale": args.scale, "fee": args.fee,
            "slip": args.slip, "price_band": [args.min_price, args.max_price],
            "min_target": args.min_target, "max_notional": args.max_notional,
            "min_notional": args.min_notional, "max_market": args.max_market,
            "max_open": args.max_open, "reserve": args.reserve,
            "synthetic_settle": not args.no_synthetic_settle,
        },
        "period": {
            "first_ts": events[0][0] if events else None,
            "last_ts": last_ts,
            "days": round((last_ts - events[0][0]) / 86400, 2) if events else 0,
        },
        "final": {
            "starting_cash": round(start_equity, 2),
            "cash": round(cash, 2),
            "invested_at_cost": round(final_invested, 2),
            "open_positions": open_count(),
            "equity": round(final_equity, 2),
            "total_pnl": round(final_equity - start_equity, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "fees_paid": round(fees_paid, 2),
            "return_pct": round(100.0 * (final_equity - start_equity)
                                / start_equity, 3),
        },
        "flow": {
            "target_trades": processed.get("trades", 0),
            "target_buys": processed.get("target_buys", 0),
            "copied_buys": processed.get("copied_buys", 0),
            "copied_sells": processed.get("copied_sells", 0),
            "redeems": processed.get("redeems", 0),
            "settles": processed.get("settles", 0),
            "merges": processed.get("merges", 0),
            "splits": processed.get("splits", 0),
            "copied_notional": round(processed.get("copied_notional", 0.0), 2),
            "skipped": dict(skipped),
            "max_invested": round(max_invested, 2),
            "max_open_positions": max_open,
        },
        "drawdown": windows,
        "daily_drawdown": {
            "max_drawdown_usd": round(daily_dd, 2),
            "max_drawdown_pct": round(100.0 * daily_dd_pct, 4),
            "peak_ts": daily_info[1] if daily_info else None,
            "trough_ts": daily_info[2] if daily_info else None,
            "days": len(daily_points),
        },
        "fresh_start_drawdown": fresh,
    }

    if args.curve_out:
        with open(args.curve_out, "w", encoding="utf-8") as fh:
            fh.write("ts,equity,cash,invested\n")
            for ts, eq, cash0, inv in curve:
                fh.write(f"{ts},{eq:.6f},{cash0:.6f},{inv:.6f}\n")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
