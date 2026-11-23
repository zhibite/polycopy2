#!/usr/bin/env python3
"""Fill-level calibration for one-sided markets: at the price he actually paid,
how often did that outcome win?

Usage: python3 fill_calibration.py positions_closed.jsonl activity.jsonl
"""
import json
import re
import sys
from collections import defaultdict

UPDOWN_RE = re.compile(r"^([a-z0-9]+)-updown-(\d+)([mhd])-(\d+)$")
WINDOW_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def updown_end(slug):
    m = UPDOWN_RE.match(slug or "")
    if not m:
        return None
    return int(m.group(4)) + int(m.group(2)) * WINDOW_SECONDS[m.group(3)]


def pct(x):
    return "-" if x is None else f"{100 * x:.1f}%"


def price_bucket(p):
    if p < .5:
        return "<50c"
    if p < .6:
        return "50-60c"
    if p < .7:
        return "60-70c"
    if p < .8:
        return "70-80c"
    if p < .9:
        return "80-90c"
    return "90-100c"


def time_bucket(mins):
    if mins is None:
        return "n/a"
    if mins < .5:
        return "<30s"
    if mins < 1:
        return "30-60s"
    if mins < 2:
        return "1-2m"
    if mins < 4:
        return "2-4m"
    return "4m+"


def main():
    closed_path = sys.argv[1] if len(sys.argv) > 1 else "positions_closed.jsonl"
    act_path = sys.argv[2] if len(sys.argv) > 2 else "activity.jsonl"

    mkt = defaultdict(list)
    with open(closed_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                c = r.get("condition_id")
                if c:
                    mkt[c].append(r)

    one_outcome = {}
    for c, rows in mkt.items():
        ois = {r.get("outcome_index") for r in rows if r.get("outcome_index") in (0, 1)}
        if len(ois) == 1:
            oi = rows[0].get("outcome_index")
            won = sum(float(r.get("realized_pnl") or 0) for r in rows) > 0
            one_outcome[c] = (oi, won)
    print(f"one-sided markets: {len(one_outcome)}")

    by_price = defaultdict(lambda: {"n": 0, "win": 0, "vol": 0.0, "sh": 0.0})
    by_time = defaultdict(lambda: defaultdict(lambda: {"n": 0, "win": 0}))
    by_both = defaultdict(lambda: defaultdict(lambda: {"n": 0, "win": 0}))
    matched = 0

    with open(act_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if (r.get("type") or "").upper() != "TRADE" or (r.get("side") or "").upper() != "BUY":
                continue
            c = r.get("condition_id")
            if c not in one_outcome:
                continue
            oi, won = one_outcome[c]
            if r.get("outcome_index") != oi:
                continue
            px = float(r.get("price") or 0)
            sh = float(r.get("size") or 0)
            usd = float(r.get("usdc_size") or 0)
            if px <= 0 or sh <= 0:
                continue
            matched += 1
            p = price_bucket(px)
            b = by_price[p]
            b["n"] += 1
            b["win"] += 1 if won else 0
            b["vol"] += usd
            b["sh"] += sh
            end = updown_end(r.get("slug"))
            tb = time_bucket((end - int(r.get("timestamp") or 0)) / 60.0 if end else None)
            t = by_time[tb][p]
            t["n"] += 1
            t["win"] += 1 if won else 0
            bb = by_both[tb][p]
            bb["n"] += 1
            bb["win"] += 1 if won else 0

    print(f"matched buy fills: {matched}\n")
    print(f"{'price':<9} {'fills':>8} {'volume':>10} {'vwap':>7} {'win':>7} {'edge':>7} {'ROI/trade':>9}")
    for p in ("<50c", "50-60c", "60-70c", "70-80c", "80-90c", "90-100c"):
        b = by_price.get(p)
        if not b or not b["n"]:
            continue
        vwap = b["vol"] / b["sh"] if b["sh"] else 0
        win = b["win"] / b["n"]
        print(f"{p:<9} {b['n']:>8} {b['vol']:>10,.0f} {vwap:>7.3f} {pct(win):>7} "
              f"{win - vwap:>+7.3f} {pct((win - vwap) / vwap if vwap else None):>9}")

    print("\nwin rate by time-to-end and price:")
    tbs = ("<30s", "30-60s", "1-2m", "2-4m", "4m+")
    ps = ("<50c", "50-60c", "60-70c", "70-80c", "80-90c", "90-100c")
    print(f"{'time':<8} " + " ".join(f"{p:>9}" for p in ps))
    for tb in tbs:
        cells = []
        for p in ps:
            d = by_both.get(tb, {}).get(p)
            cells.append(f"{pct(d['win'] / d['n']):>9}" if d and d["n"] >= 20 else f"{'-':>9}")
        print(f"{tb:<8} " + " ".join(cells))
    print(f"{'fills':<8} " + " ".join(
        f"{sum(by_both.get(tb, {}).get(p, {}).get('n', 0) for tb in tbs):>9}" for p in ps))


if __name__ == "__main__":
    main()
