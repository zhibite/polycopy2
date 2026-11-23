#!/usr/bin/env python3
"""Split closed positions into one-sided vs two-sided markets and check calibration.

Usage: python3 deep_dive_positions.py positions_closed.jsonl
"""
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

UPDOWN_RE = re.compile(r"^([a-z0-9]+)-updown-(\d+)([mhd])-(\d+)$")
WINDOW_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def updown_end(slug):
    m = UPDOWN_RE.match(slug or "")
    if not m:
        return None
    return int(m.group(4)) + int(m.group(2)) * WINDOW_SECONDS[m.group(3)]


def q(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[k]


def fs(x):
    if x is None:
        return "-"
    ax = abs(x)
    if ax >= 1e6:
        return f"${x / 1e6:.2f}M"
    if ax >= 1e3:
        return f"${x / 1e3:.1f}K"
    return f"${x:.2f}" if ax < 10 else f"${x:.0f}"


def pct(x):
    return "-" if x is None else f"{100 * x:.1f}%"


def month(t):
    return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "positions_closed.jsonl"
    mkt = defaultdict(list)
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            c = r.get("condition_id")
            if c:
                mkt[c].append(r)

    groups = {"one-sided": [], "two-sided": [], "other": []}
    for cond, rows in mkt.items():
        ois = {p.get("outcome_index") for p in rows if p.get("outcome_index") in (0, 1)}
        if len(ois) == 2:
            groups["two-sided"].append(rows)
        elif len(ois) == 1:
            groups["one-sided"].append(rows)
        else:
            groups["other"].append(rows)

    for label, setrows in groups.items():
        pnls = [sum(float(p.get("realized_pnl") or 0) for p in rows) for rows in setrows]
        if not pnls:
            continue
        wins = sum(1 for x in pnls if x > 0)
        print(f"\n=== {label}: {len(setrows)} markets, PnL {fs(sum(pnls))}, "
              f"win {pct(wins / len(pnls))}, median PnL {fs(q(pnls, .5))}")
        # entry-price calibration of the single leg (one-sided) / both legs (two-sided)
        buckets = [(0, .1, "0-10c"), (.1, .2, "10-20c"), (.2, .3, "20-30c"), (.3, .4, "30-40c"),
                   (.4, .5, "40-50c"), (.5, .6, "50-60c"), (.6, .7, "60-70c"), (.7, .8, "70-80c"),
                   (.8, .9, "80-90c"), (.9, 1.01, "90-100c")]
        print("  entry price -> win rate / PnL (one row per market, avg over legs):")
        for lo, hi, lab in buckets:
            sel = []
            for rows in setrows:
                pxs = [float(p.get("avg_price") or 0) for p in rows if p.get("avg_price")]
                if not pxs:
                    continue
                px = sum(pxs) / len(pxs)
                if lo <= px < hi:
                    sel.append((px, sum(float(p.get("realized_pnl") or 0) for p in rows)))
            if len(sel) < 30:
                continue
            w = sum(1 for _, p in sel if p > 0) / len(sel)
            avgpx = sum(px for px, _ in sel) / len(sel)
            pnl = sum(p for _, p in sel)
            print(f"    {lab:<8} n={len(sel):>6}  avg px {avgpx:.3f}  win {pct(w)}  "
                  f"edge {pct(w - avgpx)}  PnL {fs(pnl):>9}")
        if label == "one-sided":
            print("  entry -> market end by entry price (up/down only):")
            for lo, hi, lab in ((0.5, .6, "50-60c"), (.6, .7, "60-70c"), (.7, .8, "70-80c"),
                                (.8, .9, "80-90c"), (.9, 1.01, "90-100c")):
                mins, costs = [], []
                for rows in setrows:
                    px = float(rows[0].get("avg_price") or 0)
                    end = updown_end(rows[0].get("slug"))
                    t0 = rows[0].get("first_entry_at")
                    if t0 and end and lo <= px < hi:
                        mins.append((end - int(t0)) / 60.0)
                        costs.append(px * float(rows[0].get("total_size") or 0))
                if len(mins) >= 30:
                    print(f"    {lab:<8} n={len(mins):>6}  median {q(mins, .5):.1f}m  "
                          f"p10 {q(mins, .1):.1f}m  p90 {q(mins, .9):.1f}m  "
                          f"<2m {pct(sum(1 for m in mins if m < 2) / len(mins))}  "
                          f"median cost {fs(q(costs, .5))}")
            # daily count of one-sided markets entered at >=50c, recent 30d
            days = Counter()
            now = max(int(r.get("last_event_at") or 0) for rows in setrows for r in rows)
            for rows in setrows:
                px = float(rows[0].get("avg_price") or 0)
                ts = int(rows[0].get("last_event_at") or 0)
                if px >= .5 and ts and ts > now - 30 * 86400:
                    days[datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")] += 1
            if days:
                print(f"  one-sided >=50c markets/day (30d): mean {sum(days.values()) / len(days):.1f}  "
                      f"max {max(days.values())}  days {len(days)}")
        # monthly
        monthly = defaultdict(float)
        for rows in setrows:
            ts = max((int(p.get("last_event_at") or 0) for p in rows), default=0)
            if ts:
                monthly[month(ts)] += sum(float(p.get("realized_pnl") or 0) for p in rows)
        print("  monthly: " + "  ".join(f"{m}:{fs(v)}" for m, v in sorted(monthly.items())))


if __name__ == "__main__":
    main()
