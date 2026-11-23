#!/usr/bin/env python3
"""Fetch public Polymarket data for a wallet address (Data API v2).

Run from a host that can reach Polymarket's APIs. Writes raw JSON / JSONL into
--out so downstream analysis is reproducible and no row-level detail is lost.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://data-api.polymarket.com/v2"
GAMMA_API = "https://gamma-api.polymarket.com"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def get_json(url, retries=5, timeout=60):
    last = None
    for attempt in range(retries):
        wait = 1.5 * (attempt + 1)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            print(f"[warn] HTTP {e.code} {url}", file=sys.stderr)
            if e.code in (400, 404):
                try:
                    print(f"       {e.read().decode('utf-8', 'replace')[:300]}", file=sys.stderr)
                except Exception:
                    pass
                return None
            if e.code == 429:
                wait = 10 * (attempt + 1)
        except Exception as e:  # timeouts, resets, malformed JSON
            last = e
            print(f"[warn] {url} -> {e}", file=sys.stderr)
        time.sleep(wait)
    print(f"[error] giving up on {url}: {last}", file=sys.stderr)
    return None


def fetch_cursor_jsonl(path, params, out_path, label, limit=1000, max_pages=400):
    """Page a cursor-paginated endpoint, streaming rows to JSONL."""
    q = dict(params)
    q["limit"] = limit
    cursor = None
    total = 0
    pages = 0
    truncated = False
    with open(out_path, "w", encoding="utf-8") as f:
        while pages < max_pages:
            page_q = dict(q)
            if cursor:
                page_q["cursor"] = cursor
            url = f"{API}{path}?{urllib.parse.urlencode(page_q)}"
            body = get_json(url)
            pages += 1
            if not isinstance(body, dict):
                break
            rows = body.get("data") or []
            if not rows:
                break
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            total += len(rows)
            pag = body.get("pagination") or {}
            print(f"[{label}] page {pages}: +{len(rows)} = {total}", file=sys.stderr)
            cursor = pag.get("next_cursor")
            if not pag.get("has_more") or not cursor:
                break
            if pages >= max_pages:
                truncated = True
            time.sleep(0.08)
    return total, truncated, pages


def count_cursor(path, params, label, limit=1000, max_pages=100):
    """Count rows across pages without keeping them."""
    q = dict(params)
    q["limit"] = limit
    cursor = None
    total = 0
    pages = 0
    while pages < max_pages:
        page_q = dict(q)
        if cursor:
            page_q["cursor"] = cursor
        url = f"{API}{path}?{urllib.parse.urlencode(page_q)}"
        body = get_json(url)
        pages += 1
        if not isinstance(body, dict):
            break
        rows = body.get("data") or []
        if not rows:
            break
        total += len(rows)
        cursor = (body.get("pagination") or {}).get("next_cursor")
        if not (body.get("pagination") or {}).get("has_more") or not cursor:
            break
        time.sleep(0.08)
    print(f"[{label}] counted {total} rows", file=sys.stderr)
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("address")
    ap.add_argument("--out", default="probe_data")
    ap.add_argument("--activity-start", type=int, default=1,
                    help="epoch seconds lower bound for activity (1 = full history)")
    ap.add_argument("--max-pages", type=int, default=400)
    ap.add_argument("--event-meta", type=int, default=120,
                    help="fetch gamma metadata for top N events by volume (0=off)")
    args = ap.parse_args()

    addr = args.address.strip().lower()
    os.makedirs(args.out, exist_ok=True)
    started = time.time()
    meta = {"address": addr, "started": int(started), "counts": {}, "truncated": {}}

    def dump(name, obj):
        path = os.path.join(args.out, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        print(f"[saved] {path} ({os.path.getsize(path)} bytes)", file=sys.stderr)

    def unwrap(name, path, params, label):
        body = get_json(f"{API}{path}?{urllib.parse.urlencode(params)}")
        data = body.get("data") if isinstance(body, dict) else None
        dump(name, data)
        print(f"[{label}] ok={data is not None}", file=sys.stderr)
        return data

    # --- small profile endpoints -------------------------------------------
    unwrap("user_stats.json", "/user-stats", {"user": addr}, "user-stats")
    unwrap("user_volume.json", "/user-volume", {"user": addr}, "user-volume")
    leaderboard = {}
    for period in ("day", "week", "month", "all"):
        leaderboard[period] = get_json(
            f"{API}/leaderboard?{urllib.parse.urlencode({'user': addr, 'time_period': period})}"
        )
        time.sleep(0.15)
    dump("leaderboard.json", leaderboard)

    # --- PnL time series ----------------------------------------------------
    pnl = {}
    for interval, fidelity in (("max", "1d"), ("1m", "1h"), ("1w", "1h"), ("1d", "1h")):
        body = get_json(
            f"{API}/user-pnl?{urllib.parse.urlencode({'user': addr, 'interval': interval, 'fidelity': fidelity})}"
        )
        pnl[interval] = body.get("data") if isinstance(body, dict) else None
        time.sleep(0.2)
    dump("pnl_history.json", pnl)

    # --- activity feed (full history) --------------------------------------
    act_path = os.path.join(args.out, "activity.jsonl")
    n_act, act_trunc, act_pages = fetch_cursor_jsonl(
        "/activity",
        {"user": addr, "start": args.activity_start, "sort_by": "TIMESTAMP", "sort_direction": "DESC"},
        act_path,
        "activity",
        max_pages=args.max_pages,
    )
    meta["counts"]["activity"] = n_act
    meta["truncated"]["activity"] = act_trunc
    meta["pages"] = {"activity": act_pages}

    # --- positions ----------------------------------------------------------
    for status, fname, label in (
        ("OPEN", "positions_open.jsonl", "positions-open"),
        ("REDEEMABLE", "positions_redeemable.jsonl", "positions-redeemable"),
        ("MERGEABLE", "positions_mergeable.jsonl", "positions-mergeable"),
        ("CLOSED", "positions_closed.jsonl", "positions-closed"),
    ):
        path = os.path.join(args.out, fname)
        n, trunc, pages = fetch_cursor_jsonl(
            "/positions",
            {"user": addr, "status": status, "sort_by": "TIMESTAMP", "sort_direction": "DESC"},
            path,
            label,
            max_pages=args.max_pages,
        )
        meta["counts"][f"positions_{status.lower()}"] = n
        meta["truncated"][f"positions_{status.lower()}"] = trunc
        meta["pages"][f"positions_{status.lower()}"] = pages

    # --- maker vs taker fill counts (last 24h) -----------------------------
    now = int(time.time())
    window = {"user": addr, "start": now - 86400}
    taker_24h = count_cursor("/trades", dict(window, taker_only="true"), "taker-24h", max_pages=60)
    all_24h = count_cursor("/trades", dict(window, taker_only="false"), "allf-24h", max_pages=120)
    meta["counts"]["taker_fills_24h"] = taker_24h
    meta["counts"]["all_fills_24h"] = all_24h

    # --- gamma metadata for the most-traded events -------------------------
    if args.event_meta > 0 and n_act:
        vol_by_event = {}
        with open(act_path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (r.get("type") or "").upper() != "TRADE":
                    continue
                slug = r.get("event_slug")
                if not slug:
                    continue
                vol_by_event[slug] = vol_by_event.get(slug, 0.0) + float(r.get("usdc_size") or 0)
        ranked = sorted(vol_by_event.items(), key=lambda kv: kv[1], reverse=True)
        top = [s for s, _ in ranked[: args.event_meta]]
        event_meta = {}
        for i, slug in enumerate(top):
            ev = get_json(
                f"{GAMMA_API}/events?slug={urllib.parse.quote(slug)}", retries=2
            )
            if isinstance(ev, list) and ev:
                e = ev[0]
                event_meta[slug] = {
                    "title": e.get("title"),
                    "tags": [t.get("label") for t in (e.get("tags") or []) if isinstance(t, dict)],
                    "volume": e.get("volume"),
                    "liquidity": e.get("liquidity"),
                    "closed": e.get("closed"),
                    "endDate": e.get("endDate"),
                    "tradedVolume": vol_by_event.get(slug),
                }
            time.sleep(0.12)
            if (i + 1) % 50 == 0:
                print(f"[events] {i + 1}/{len(top)}", file=sys.stderr)
        dump("event_meta.json", event_meta)
        meta["counts"]["events_meta"] = len(event_meta)
        meta["distinct_events"] = len(vol_by_event)

    meta["finished"] = int(time.time())
    meta["elapsed_s"] = round(time.time() - started, 1)
    dump("meta.json", meta)
    print(f"[done] {time.time() - started:.1f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
