#!/usr/bin/env python3
"""Last-N-days profile for the f705 probe dataset.

Reads activity.jsonl / positions_closed.jsonl / positions_open.jsonl /
pnl_history.json from a fetch_wallet.py probe dir and prints a text report.

Usage: python3 f705_month.py probe_f705 [--days 30] [--end <epoch>]
"""
import argparse
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

ASSETS = [
    ("Bitcoin", re.compile(r"bitcoin|\bbtc\b", re.I)),
    ("Ethereum", re.compile(r"ethereum|\beth\b", re.I)),
    ("Solana", re.compile(r"solana|\bsol\b", re.I)),
    ("XRP", re.compile(r"\bxrp\b|ripple", re.I)),
    ("Dogecoin", re.compile(r"dogecoin|\bdoge\b", re.I)),
]


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def fs(x):
    if x is None:
        return "-"
    ax = abs(x)
    if ax >= 1e6:
        return f"${x / 1e6:,.2f}M"
    if ax >= 1e3:
        return f"${x / 1e3:,.1f}K"
    return f"${x:,.2f}"


def pct(x, nd=1):
    return "-" if x is None else f"{100 * x:.{nd}f}%"


def q(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))]


def dt(t):
    return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%d %H:%M")


def day(t):
    return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%d")


def asset_of(title, slug=""):
    for name, rx in ASSETS:
        if rx.search(title or "") or rx.search(slug or ""):
            return name
    return "Other"


def price_bucket(p):
    edges = [0, 0.05, 0.15, 0.30, 0.50, 0.70, 0.85, 0.95, 1.0001]
    for lo, hi in zip(edges, edges[1:]):
        if lo <= p < hi:
            return f"{lo*100:.0f}-{hi*100:.0f}c"
    return "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("probe")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--end", type=int, default=None)
    args = ap.parse_args()

    act = load_jsonl(os.path.join(args.probe, "activity.jsonl"))
    closed = load_jsonl(os.path.join(args.probe, "positions_closed.jsonl"))
    openp = load_jsonl(os.path.join(args.probe, "positions_open.jsonl"))
    pnl = json.load(open(os.path.join(args.probe, "pnl_history.json"), encoding="utf-8"))

    trades_all = [r for r in act if (r.get("type") or "").upper() == "TRADE"]
    end = args.end or max(int(r["timestamp"]) for r in act)
    cutoff = end - args.days * 86400

    print("=" * 78)
    print(f"f705 LAST {args.days} DAYS  {dt(cutoff)} .. {dt(end)} UTC")
    print("=" * 78)

    # ---------------- PnL from Polymarket pnl history (hourly 1m series) -------
    pts = sorted(pnl["1m"]["points"], key=lambda x: x["timestamp"])
    base = next((p for p in pts if p["timestamp"] >= cutoff), pts[0])
    last = pts[-1]

    def d(k, nd=0):
        return (last.get(k) or 0) - (base.get(k) or 0)

    print("\n--- PROFIT (economic PnL, Polymarket pnl history) -------------------")
    print(f"  window base point          : {dt(base['timestamp'])}")
    print(f"  window end point           : {dt(last['timestamp'])}")
    print(f"  economic PnL               : {fs(d('economic_pnl'))}")
    print(f"  trade PnL                  : {fs(d('trade_pnl'))}")
    print(f"  realized market PnL        : {fs(d('realized_market_pnl'))}")
    print(f"  unrealized PnL change      : {fs(d('unrealized_pnl'))}")
    print(f"  fees                       : {fs(d('fees'))}")
    print(f"  taker rebate               : {fs(d('taker_rebate'))}")
    print(f"  maker rebate               : {fs(d('maker_rebate'))}")
    print(f"  fees refunded              : {fs(d('fees_refunded'))}")
    print(f"  yield income               : {fs(d('yield_income'))}")
    print(f"  volume                     : {fs(d('volume'))}")
    print(f"  trade count                : {d('trade_count'):,.0f}")
    print(f"  PnL / volume               : {pct(d('economic_pnl') / d('volume') if d('volume') else None, 2)}")
    print(f"  unrealized PnL now         : {fs(last.get('unrealized_pnl'))}")

    # max drawdown on the hourly economic PnL curve inside the window
    win = [p for p in pts if p["timestamp"] >= base["timestamp"]]
    peak = win[0]["economic_pnl"]
    peak_ts = win[0]["timestamp"]
    worst_dd = 0.0
    dd_at = None
    for p in win:
        v = p["economic_pnl"]
        if v > peak:
            peak, peak_ts = v, p["timestamp"]
        dd = v - peak
        if dd < worst_dd:
            worst_dd, dd_at = dd, (peak_ts, p["timestamp"])
    print(f"  max drawdown in window     : {fs(worst_dd)}"
          + (f"  ({dt(dd_at[0])} peak -> {dt(dd_at[1])})" if dd_at else ""))

    # daily PnL table
    by_day_last = {}
    for p in win:
        by_day_last[day(p["timestamp"])] = p
    days = sorted(by_day_last)
    prev = base
    daily = []
    for dd in days:
        p = by_day_last[dd]
        daily.append((
            dd,
            (p.get("economic_pnl") or 0) - (prev.get("economic_pnl") or 0),
            (p.get("volume") or 0) - (prev.get("volume") or 0),
            (p.get("trade_count") or 0) - (prev.get("trade_count") or 0),
        ))
        prev = p
    print("\n  daily economic PnL / volume / trades:")
    for dd, pnl_d, vol_d, n_d in daily:
        print(f"    {dd}  pnl {fs(pnl_d):>10}  volume {fs(vol_d):>10}  trades {n_d:>7,.0f}")
    ups = sorted(daily, key=lambda x: x[1], reverse=True)
    print("  best days                  : " + ", ".join(f"{x[0]} {fs(x[1])}" for x in ups[:3]))
    print("  worst days                 : " + ", ".join(f"{x[0]} {fs(x[1])}" for x in ups[-3:]))
    pos_days = sum(1 for x in daily if x[1] > 0)
    print(f"  positive days              : {pos_days}/{len(daily)} ({pct(pos_days / len(daily))})")

    # weekly buckets
    wk = defaultdict(lambda: [0.0, 0.0, 0.0])
    prev = base
    for dd in days:
        p = by_day_last[dd]
        key = datetime.strptime(dd, "%Y-%m-%d").strftime("%G-W%V")
        wk[key][0] += (p.get("economic_pnl") or 0) - (prev.get("economic_pnl") or 0)
        wk[key][1] += (p.get("volume") or 0) - (prev.get("volume") or 0)
        wk[key][2] += (p.get("trade_count") or 0) - (prev.get("trade_count") or 0)
        prev = p
    print("\n  weekly buckets:")
    for k, (v0, v1, v2) in wk.items():
        print(f"    {k}  pnl {fs(v0):>10}  volume {fs(v1):>10}  trades {v2:>7,.0f}")

    # ---------------- activity in window -------------------------------------
    actw = [r for r in act if int(r.get("timestamp") or 0) >= cutoff]
    tw = [r for r in actw if (r.get("type") or "").upper() == "TRADE"]
    types = Counter((r.get("type") or "").upper() for r in actw)
    buys = [r for r in tw if (r.get("side") or "").upper() == "BUY"]
    sells = [r for r in tw if (r.get("side") or "").upper() == "SELL"]

    def usd(r):
        return float(r.get("usdc_size") or 0)

    def px(r):
        return float(r.get("price") or 0)

    def sz(r):
        return float(r.get("size") or 0)

    print("\n--- TRADING BEHAVIOUR IN WINDOW ------------------------------------")
    print(f"  activity rows              : {len(actw):,}  " + " ".join(f"{k}={v}" for k, v in types.most_common()))
    print(f"  trade fills                : {len(tw):,}  ({(len(tw) / args.days):.1f}/day)")
    print(f"  BUY fills                  : {len(buys):,}  {fs(sum(usd(r) for r in buys))}")
    print(f"  SELL fills                 : {len(sells):,}  {fs(sum(usd(r) for r in sells))}")
    print(f"  trade cash (buy+sell)      : {fs(sum(usd(r) for r in tw))}")
    print(f"  distinct conditions        : {len({r.get('condition_id') for r in tw}):,}")
    print(f"  distinct events            : {len({r.get('event_slug') for r in tw}):,}")
    outcomes = Counter((r.get("outcome") or "?") for r in tw)
    print("  outcomes                   : " + " ".join(f"{k}={v}" for k, v in outcomes.most_common()))

    fill_usd = [usd(r) for r in tw]
    fill_sh = [sz(r) for r in tw]
    print(f"  fill notional              : p10 {fs(q(fill_usd, .1))}  p25 {fs(q(fill_usd, .25))}  "
          f"p50 {fs(q(fill_usd, .5))}  p75 {fs(q(fill_usd, .75))}  p90 {fs(q(fill_usd, .9))}  "
          f"p99 {fs(q(fill_usd, .99))}  max {fs(max(fill_usd))}")
    print(f"  fill shares                : p50 {q(fill_sh, .5):,.0f}  p90 {q(fill_sh, .9):,.0f}  max {max(fill_sh):,.0f}")
    bands = [("<$10", 0, 10), ("$10-50", 10, 50), ("$50-100", 50, 100), ("$100-500", 100, 500),
             ("$500-1K", 500, 1000), ("$1K-5K", 1000, 5000), (">$5K", 5000, 1e18)]
    print("  fill notional buckets:")
    for name, lo, hi in bands:
        sel = [r for r in tw if lo <= usd(r) < hi]
        print(f"    {name:<8} {len(sel):>7,} ({pct(len(sel)/len(tw)):>5})  volume {fs(sum(usd(r) for r in sel)):>10}")

    print("  BUY price distribution (by count / by USD):")
    edges = [0, .05, .15, .30, .50, .70, .85, .95, 1.0001]
    tot_b = sum(usd(r) for r in buys)
    for lo, hi in zip(edges, edges[1:]):
        sel = [r for r in buys if lo <= px(r) < hi]
        v = sum(usd(r) for r in sel)
        print(f"    {lo*100:>3.0f}-{hi*100:>3.0f}c  {len(sel):>7,} ({pct(len(sel)/len(buys)):>5})  "
              f"USD {fs(v):>10} ({pct(v/tot_b if tot_b else 0):>5})")
    print(f"  share-weighted avg BUY px  : {sum(px(r)*sz(r) for r in buys)/sum(sz(r) for r in buys):.4f}")
    print("  SELL price distribution (by count):")
    for lo, hi in zip(edges, edges[1:]):
        sel = [r for r in sells if lo <= px(r) < hi]
        print(f"    {lo*100:>3.0f}-{hi*100:>3.0f}c  {len(sel):>7,} ({pct(len(sel)/len(sells)):>5})")

    # hour / weekday
    hours = Counter(datetime.fromtimestamp(int(r["timestamp"]), timezone.utc).hour for r in tw)
    print("  fills by UTC hour          : " + " ".join(f"{h:02d}={hours[h]}" for h in range(24)))
    wd = Counter(datetime.fromtimestamp(int(r["timestamp"]), timezone.utc).strftime("%a") for r in tw)
    print("  fills by weekday           : " + " ".join(f"{k}={v}" for k, v in wd.most_common()))

    # assets / events / titles
    asset_usd = defaultdict(float)
    asset_n = Counter()
    for r in tw:
        a = asset_of(r.get("title"), r.get("slug"))
        asset_usd[a] += usd(r)
        asset_n[a] += 1
    print("  asset mix by volume        : " + "  ".join(
        f"{a} {fs(asset_usd[a])} ({pct(asset_usd[a]/sum(asset_usd.values()))}, {asset_n[a]} fills)"
        for a in sorted(asset_usd, key=asset_usd.get, reverse=True)))
    ev_usd = defaultdict(float)
    ev_n = Counter()
    for r in tw:
        ev_usd[r.get("event_slug") or "?"] += usd(r)
        ev_n[r.get("event_slug") or "?"] += 1
    print("  top events by volume:")
    for e, v in sorted(ev_usd.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"    {pct(v/sum(ev_usd.values())):>5}  {fs(v):>9}  fills {ev_n[e]:>5}  {e}")
    tit_usd = defaultdict(float)
    tit_n = Counter()
    for r in tw:
        tit_usd[r.get("title") or "?"] += usd(r)
        tit_n[r.get("title") or "?"] += 1
    print("  top markets by volume:")
    for t, v in sorted(tit_usd.items(), key=lambda x: x[1], reverse=True)[:12]:
        print(f"    {pct(v/sum(tit_usd.values())):>5}  {fs(v):>9}  fills {tit_n[t]:>5}  {t}")

    # both-outcome behaviour at condition level
    cond_sides = defaultdict(set)
    for r in tw:
        cond_sides[r.get("condition_id")].add((r.get("outcome_index"), (r.get("side") or "").upper()))
    both = 0
    for cid, s in cond_sides.items():
        outs_bought = {oi for oi, side in s if side == "BUY"}
        if len(outs_bought) > 1:
            both += 1
    print(f"  conditions with BUY on both outcomes: {both}/{len(cond_sides)} ({pct(both/len(cond_sides))})")

    # copy-signal profile (our paper rules: BUY 0.30-0.70, target USD >= 20, 1%)
    sig = [r for r in buys if 0.30 <= px(r) <= 0.70 and usd(r) >= 20]
    our = [min(100.0, max(2.0, 0.01 * usd(r))) for r in sig]
    print("\n  COPY-SIGNAL PROFILE (BUY 30-70c, target >= $20):")
    print(f"    signals                  : {len(sig):,}  ({len(sig)/args.days:.1f}/day)")
    print(f"    target notional          : {fs(sum(usd(r) for r in sig))}")
    print(f"    our notional at 1%       : {fs(sum(our))}  median {fs(q(our, .5))}")
    print(f"    avg BUY price in band    : {sum(px(r)*sz(r) for r in sig)/sum(sz(r) for r in sig):.4f}")
    print(f"    share of all BUY fills   : {pct(len(sig)/len(buys))} count / {pct(sum(usd(r) for r in sig)/tot_b if tot_b else 0)} USD")

    # ---------------- closed positions closed in window ----------------------
    cw = [r for r in closed if int(r.get("last_event_at") or 0) >= cutoff]
    print("\n--- CLOSED POSITIONS CLOSED IN WINDOW ------------------------------")
    print(f"  positions closed           : {len(cw):,}")
    pnls = [float(r.get("realized_pnl") or 0) for r in cw]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    total = sum(pnls)
    print(f"  realized PnL               : {fs(total)}")
    print(f"  win rate                   : {pct(len(wins)/len(cw) if cw else None)}  ({len(wins)}W / {len(losses)}L)")
    print(f"  avg / median win           : {fs(sum(wins)/len(wins) if wins else 0)} / {fs(q(wins, .5))}")
    print(f"  avg / median loss          : {fs(sum(losses)/len(losses) if losses else 0)} / {fs(q(losses, .5))}")
    print(f"  profit factor              : {(sum(wins)/abs(sum(losses)) if losses and sum(losses) else float('inf')):.2f}")
    holds = [(int(r.get("last_event_at") or 0) - int(r.get("first_entry_at") or 0)) / 3600
             for r in cw if r.get("first_entry_at") and r.get("last_event_at")]
    if holds:
        print(f"  hold hours (first->close)  : p25 {q(holds, .25):.1f}  median {q(holds, .5):.1f}  "
              f"p75 {q(holds, .75):.1f}  p90 {q(holds, .9):.1f}")
    # PnL by avg entry price
    print("  PnL by avg entry price:")
    for lo, hi, label in [(0, .30, "0-30c"), (.30, .50, "30-50c"), (.50, .70, "50-70c"),
                          (.70, .85, "70-85c"), (.85, .95, "85-95c"), (.95, 1.01, "95-100c")]:
        sel = [r for r in cw if lo <= float(r.get("avg_price") or 0) < hi]
        if not sel:
            continue
        s = sum(float(r.get("realized_pnl") or 0) for r in sel)
        w = sum(1 for r in sel if float(r.get("realized_pnl") or 0) > 0)
        print(f"    {label:<8} n={len(sel):>4}  pnl {fs(s):>10}  win {pct(w/len(sel)):>6}  avg {fs(s/len(sel))}")
    # PnL by asset
    print("  PnL by asset:")
    apnl = defaultdict(float)
    an = Counter()
    aw = Counter()
    for r in cw:
        a = asset_of(r.get("title"), r.get("slug"))
        apnl[a] += float(r.get("realized_pnl") or 0)
        an[a] += 1
        aw[a] += 1 if float(r.get("realized_pnl") or 0) > 0 else 0
    for a in sorted(apnl, key=apnl.get, reverse=True):
        print(f"    {a:<10} n={an[a]:>4}  pnl {fs(apnl[a]):>10}  win {pct(aw[a]/an[a]):>6}")
    print("  best closed positions:")
    for r in sorted(cw, key=lambda r: float(r.get("realized_pnl") or 0), reverse=True)[:10]:
        print(f"    {fs(float(r.get('realized_pnl') or 0)):>10}  {r.get('outcome','?'):<4} "
              f"avg {float(r.get('avg_price') or 0):.3f}  {r.get('title','')}")
    print("  worst closed positions:")
    for r in sorted(cw, key=lambda r: float(r.get("realized_pnl") or 0))[:10]:
        print(f"    {fs(float(r.get('realized_pnl') or 0)):>10}  {r.get('outcome','?'):<4} "
              f"avg {float(r.get('avg_price') or 0):.3f}  {r.get('title','')}")
    # close mechanism proxy
    mech = Counter()
    for r in cw:
        if r.get("mergeable") and not r.get("redeemable"):
            mech["MERGE"] += 1
        elif r.get("redeemable"):
            mech["REDEEM"] += 1
        elif float(r.get("current_size") or 0) == 0:
            mech["SELL/closed"] += 1
        else:
            mech["?"] += 1
    print("  close path flags           : " + " ".join(f"{k}={v}" for k, v in mech.most_common()))

    # positions opened in window that are still open
    opw = [r for r in openp if int(r.get("first_entry_at") or 0) >= cutoff]
    print(f"\n  open positions opened in window: {len(opw):,}  "
          f"cost {fs(sum(float(r.get('total_cost_usdc') or 0) for r in opw))}  "
          f"unrealized {fs(sum(float(r.get('unrealized_pnl') or 0) for r in opw))}")

    # ---------------- open book now ------------------------------------------
    print("\n--- OPEN BOOK NOW ---------------------------------------------------")
    cost = sum(float(r.get("total_cost_usdc") or r.get("entry_cost_usdc") or 0) for r in openp)
    cur = sum(float(r.get("current_value") or 0) for r in openp)
    unr = sum(float(r.get("unrealized_pnl") or 0) for r in openp)
    print(f"  open positions             : {len(openp):,}")
    print(f"  cost / current / unrealized: {fs(cost)} / {fs(cur)} / {fs(unr)}")
    print("  largest open positions:")
    for r in sorted(openp, key=lambda r: float(r.get("current_value") or 0), reverse=True)[:10]:
        print(f"    {fs(float(r.get('current_value') or 0)):>9}  avg {float(r.get('avg_price') or 0):.3f} "
              f"-> {float(r.get('current_price') or 0):.3f}  {r.get('outcome','?'):<4}  {r.get('title','')}")

    # ---------------- last 24h / tail ----------------------------------------
    day_ago = end - 86400
    t24 = [r for r in trades_all if int(r["timestamp"]) >= day_ago]
    print("\n--- LAST 24H ---------------------------------------------------------")
    print(f"  fills                      : {len(t24):,}  volume {fs(sum(usd(r) for r in t24))}")
    print(f"  BUY / SELL                 : {sum(1 for r in t24 if r.get('side')=='BUY')} / "
          f"{sum(1 for r in t24 if r.get('side')=='SELL')}")
    if t24:
        print(f"  last fill                  : {dt(max(int(r['timestamp']) for r in t24))}")
        print(f"  last fill detail           : {t24[0].get('side')} {t24[0].get('outcome')} "
              f"@ {float(t24[0].get('price')):.3f}  {t24[0].get('title')}")


if __name__ == "__main__":
    main()
