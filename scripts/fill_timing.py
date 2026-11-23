#!/usr/bin/env python3
"""Buy-fill timing vs market end, split by fill price and window.

Usage: python3 fill_timing.py probe_data/activity.jsonl
"""
import json
import re
import sys
from collections import defaultdict

UPDOWN_RE = re.compile(r"^([a-z0-9]+)-updown-(\d+)([mhd])-(\d+)$")
WINDOW_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def q(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[k]


def pct(x):
    return "-" if x is None else f"{100 * x:.1f}%"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "activity.jsonl"
    data = defaultdict(lambda: defaultdict(lambda: {"mins": [], "usd": 0.0}))
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if (r.get("type") or "").upper() != "TRADE" or (r.get("side") or "").upper() != "BUY":
                continue
            m = UPDOWN_RE.match(r.get("slug") or "")
            if not m:
                continue
            end = int(m.group(4)) + int(m.group(2)) * WINDOW_SECONDS[m.group(3)]
            mins = (end - int(r.get("timestamp") or 0)) / 60.0
            price = float(r.get("price") or 0)
            window = m.group(2) + m.group(3)
            if price < .5:
                lab = "<50c"
            elif price < .6:
                lab = "50-60c"
            elif price < .7:
                lab = "60-70c"
            elif price < .8:
                lab = "70-80c"
            elif price < .9:
                lab = "80-90c"
            else:
                lab = "90-100c"
            d = data[window][lab]
            d["mins"].append(mins)
            d["usd"] += float(r.get("usdc_size") or 0)

    for window in ("5m", "15m", "5m+15m"):
        if window == "5m+15m":
            merged = defaultdict(lambda: {"mins": [], "usd": 0.0})
            for w in ("5m", "15m"):
                for lab, d in data[w].items():
                    merged[lab]["mins"].extend(d["mins"])
                    merged[lab]["usd"] += d["usd"]
            bucket_map = merged
        else:
            bucket_map = data.get(window) or {}
        if not bucket_map:
            continue
        print(f"\n=== {window} buy fills: minutes from fill to market end")
        print(f"  {'price':<9} {'fills':>8} {'volume':>9} {'median':>7} {'p10':>6} {'p90':>6} {'<2m':>6} {'<30s':>6}")
        for lab in ("<50c", "50-60c", "60-70c", "70-80c", "80-90c", "90-100c"):
            d = bucket_map.get(lab)
            if not d or not d["mins"]:
                continue
            mins = d["mins"]
            print(f"  {lab:<9} {len(mins):>8} {d['usd']:>9,.0f} {q(mins, .5):>7.1f} "
                  f"{q(mins, .1):>6.1f} {q(mins, .9):>6.1f} "
                  f"{pct(sum(1 for m in mins if m < 2) / len(mins)):>6} "
                  f"{pct(sum(1 for m in mins if m < .5) / len(mins)):>6}")


if __name__ == "__main__":
    main()
