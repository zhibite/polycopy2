#!/usr/bin/env python3
"""Unconditional fill-level calibration: mirror every BUY fill at his price and
hold to resolution. Winning outcome taken from closed positions' current_price.

Usage: python3 fill_calibration_all.py positions_closed.jsonl activity.jsonl
"""
import json
import sys
from collections import defaultdict


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


def main():
    closed_path = sys.argv[1] if len(sys.argv) > 1 else "positions_closed.jsonl"
    act_path = sys.argv[2] if len(sys.argv) > 2 else "activity.jsonl"

    won = defaultdict(dict)
    distinct_oi = defaultdict(set)
    with open(closed_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            c = r.get("condition_id")
            oi = r.get("outcome_index")
            if not c or oi not in (0, 1):
                continue
            distinct_oi[c].add(oi)
            cp = r.get("current_price")
            try:
                cp = float(cp)
            except (TypeError, ValueError):
                continue
            if cp in (0.0, 1.0):
                won[c][oi] = int(cp)

    def group(c):
        ois = distinct_oi.get(c) or set()
        if len(ois) >= 2:
            return "two-sided"
        if len(ois) == 1:
            return "one-sided"
        return "unknown"

    stats = defaultdict(lambda: {"n": 0, "win": 0, "vol": 0.0, "sh": 0.0})
    matched = 0
    unknown = 0
    with open(act_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if (r.get("type") or "").upper() != "TRADE" or (r.get("side") or "").upper() != "BUY":
                continue
            c = r.get("condition_id")
            oi = r.get("outcome_index")
            if not c or oi not in (0, 1) or oi not in won.get(c, {}):
                unknown += 1
                continue
            px = float(r.get("price") or 0)
            sh = float(r.get("size") or 0)
            usd = float(r.get("usdc_size") or 0)
            if px <= 0 or sh <= 0:
                continue
            matched += 1
            w = won[c][oi]
            for key in ("ALL", group(c), f"ALL|{price_bucket(px)}",
                        f"{group(c)}|{price_bucket(px)}"):
                s = stats[key]
                s["n"] += 1
                s["win"] += w
                s["vol"] += usd
                s["sh"] += sh

    print(f"matched fills {matched}, unmatched {unknown}\n")
    for grp in ("ALL", "one-sided", "two-sided"):
        print(f"=== {grp}")
        print(f"  {'price':<9} {'fills':>8} {'volume':>10} {'vwap':>7} {'win':>7} "
              f"{'edge':>7} {'ROI':>7} {'+1c ROI':>8}")
        for p in ("<50c", "50-60c", "60-70c", "70-80c", "80-90c", "90-100c"):
            s = stats.get(f"{grp}|{p}")
            if not s or not s["n"]:
                continue
            vwap = s["vol"] / s["sh"] if s["sh"] else 0
            win = s["win"] / s["n"]
            edge = win - vwap
            roi = edge / vwap if vwap else 0
            roi1 = (win - (vwap + .01)) / (vwap + .01) if vwap else 0
            print(f"  {p:<9} {s['n']:>8} {s['vol']:>10,.0f} {vwap:>7.3f} {pct(win):>7} "
                  f"{edge:>+7.3f} {pct(roi):>7} {pct(roi1):>8}")
        print()
    if "ALL" in stats:
        s = stats["ALL"]
        vwap = s["vol"] / s["sh"]
        print(f"ALL: n={s['n']} vwap={vwap:.3f} win={pct(s['win']/s['n'])} "
              f"edge={s['win']/s['n']-vwap:+.3f}")


if __name__ == "__main__":
    main()
