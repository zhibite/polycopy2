#!/usr/bin/env python3
"""Compare a data-api paper run with an on-chain paper run.

Both databases are opened read-only.  Signals are paired by
(transaction hash, side, token id); latency and paper-fill slippage are then
compared for the same underlying fill.

Usage:
    python3 compare_sources.py data/paper_f705.db data/paper_f705_onchain.db
    python3 compare_sources.py A.db B.db --json-out data/compare.json --verbose
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from typing import Any


def open_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_signals(path: str) -> list[dict[str, Any]]:
    conn = open_ro(path)
    try:
        rows = conn.execute("SELECT * FROM signals ORDER BY id").fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.setdefault("source", "data_api")
        key = str(item.get("dedupe_key") or "")
        item["tx"] = key.split("|")[0]
        out.append(item)
    return out


def load_start(path: str) -> int | None:
    """The account creation time, used as the on-chain run's start line."""
    conn = open_ro(path)
    try:
        row = conn.execute(
            "SELECT created_at FROM account WHERE id = 1").fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    return int(row[0]) if row and row[0] is not None else None


def index(signals: list[dict[str, Any]]) -> dict[tuple[str, str, str],
                                                list[dict[str, Any]]]:
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        if not signal.get("tx"):
            continue
        key = (str(signal["tx"]).lower(), str(signal.get("side") or "").upper(),
               str(signal.get("token_id") or ""))
        buckets[key].append(signal)
    return buckets


def pair_signals(a_signals: list[dict[str, Any]],
                 b_signals: list[dict[str, Any]]) -> list[tuple[dict[str, Any],
                                                                dict[str, Any]]]:
    a_index, b_index = index(a_signals), index(b_signals)
    used: set[int] = set()
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for key, a_rows in a_index.items():
        b_rows = b_index.get(key, [])
        for a_row in a_rows:
            candidates = [row for row in b_rows if id(row) not in used]
            if not candidates:
                continue
            b_row = min(
                candidates,
                key=lambda row: abs(float(row.get("size") or 0)
                                    - float(a_row.get("size") or 0)),
            )
            used.add(id(b_row))
            pairs.append((a_row, b_row))
    return pairs


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def adverse_slippage(signal: dict[str, Any]) -> float | None:
    """Per-share slippage vs the target's fill; positive is worse for us."""
    price = signal.get("price")
    our = signal.get("our_price")
    if price is None or our is None:
        return None
    if str(signal.get("side") or "").upper() == "BUY":
        return float(our) - float(price)
    return float(price) - float(our)


def summarize(name: str, signals: list[dict[str, Any]]) -> dict[str, Any]:
    copied = [signal for signal in signals if signal.get("copied")]
    latencies = [float(signal["latency_seconds"]) for signal in signals
                 if signal.get("latency_seconds") is not None]
    return {
        "name": name,
        "signals": len(signals),
        "copied": len(copied),
        "latency_median": quantile(latencies, 0.5),
        "latency_p90": quantile(latencies, 0.9),
        "latency_max": max(latencies) if latencies else None,
        "notional": sum(float(signal.get("our_notional") or 0)
                        for signal in copied),
        "fees": sum(float(signal.get("our_fee") or 0) for signal in copied),
        "reasons": dict(Counter(
            str(signal.get("reason") or "copied")
            for signal in signals if not signal.get("copied")
        ).most_common()),
    }


def compare(a_path: str, b_path: str, *,
            verbose: bool = False, since: int | None = None) -> dict[str, Any]:
    a_signals = load_signals(a_path)
    b_signals = load_signals(b_path)
    if since is None:
        since = load_start(b_path)
    excluded = 0
    if since:
        before = len(a_signals)
        a_signals = [signal for signal in a_signals
                     if int(signal.get("created_at") or 0) >= since]
        excluded = before - len(a_signals)
    pairs = pair_signals(a_signals, b_signals)

    latency_pairs = [
        (float(a["latency_seconds"]), float(b["latency_seconds"]))
        for a, b in pairs
        if a.get("latency_seconds") is not None
        and b.get("latency_seconds") is not None
    ]
    lat_a = [a for a, _ in latency_pairs]
    lat_b = [b for _, b in latency_pairs]
    leads = [a - b for a, b in latency_pairs]
    created_lead = [
        int(a.get("created_at") or 0) - int(b.get("created_at") or 0)
        for a, b in pairs if a.get("created_at") and b.get("created_at")
    ]

    both_copied = [(a, b) for a, b in pairs
                   if a.get("copied") and b.get("copied")]
    slip_pairs: list[tuple[float, float]] = []
    for a, b in both_copied:
        slip_a = adverse_slippage(a)
        slip_b = adverse_slippage(b)
        if slip_a is not None and slip_b is not None:
            slip_pairs.append((slip_a, slip_b))
    slip_a = [a for a, _ in slip_pairs]
    slip_b = [b for _, b in slip_pairs]
    improvements = [a - b for a, b in slip_pairs]

    b_matched = {id(b) for _, b in pairs}
    a_matched = {id(a) for a, _ in pairs}
    only_b = [signal for signal in b_signals if id(signal) not in b_matched]
    only_a = [signal for signal in a_signals if id(signal) not in a_matched]

    result = {
        "data_api": {"path": a_path, **summarize("data_api", a_signals)},
        "onchain": {"path": b_path, **summarize("onchain", b_signals)},
        "since": since,
        "excluded_data_api_before_start": excluded,
        "matched": len(pairs),
        "matched_latency": {
            "data_api_median": quantile(lat_a, 0.5),
            "data_api_p90": quantile(lat_a, 0.9),
            "onchain_median": quantile(lat_b, 0.5),
            "onchain_p90": quantile(lat_b, 0.9),
            "lead_median": quantile(leads, 0.5),
            "lead_p90": quantile(leads, 0.9),
            "lead_max": max(leads) if leads else None,
            "created_lead_median": quantile(
                [float(v) for v in created_lead], 0.5),
        },
        "both_copied": len(both_copied),
        "slippage": {
            "data_api_median": quantile(slip_a, 0.5),
            "onchain_median": quantile(slip_b, 0.5),
            "data_api_mean": (sum(slip_a) / len(slip_a)) if slip_a else None,
            "onchain_mean": (sum(slip_b) / len(slip_b)) if slip_b else None,
            "improvement_median": quantile(improvements, 0.5),
            "improvement_mean": (sum(improvements) / len(improvements)
                                 if improvements else None),
        },
        "only_data_api": len(only_a),
        "only_onchain": len(only_b),
        "pairs": [
            {
                "tx": a.get("tx"),
                "side": a.get("side"),
                "size": a.get("size"),
                "signal_price": a.get("price"),
                "api_latency": a.get("latency_seconds"),
                "chain_latency": b.get("latency_seconds"),
                "api_copied": bool(a.get("copied")),
                "chain_copied": bool(b.get("copied")),
                "api_reason": a.get("reason"),
                "chain_reason": b.get("reason"),
                "api_price": a.get("our_price"),
                "chain_price": b.get("our_price"),
                "api_slip": adverse_slippage(a),
                "chain_slip": adverse_slippage(b),
            }
            for a, b in pairs
        ],
        "only_data_api_samples": [
            {"tx": signal.get("tx"), "side": signal.get("side"),
             "size": signal.get("size"), "reason": signal.get("reason")}
            for signal in only_a[:20]
        ],
        "only_onchain_samples": [
            {"tx": signal.get("tx"), "side": signal.get("side"),
             "size": signal.get("size"), "reason": signal.get("reason")}
            for signal in only_b[:20]
        ],
    }
    return result


def fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def format_report(result: dict[str, Any], *, verbose: bool = False) -> str:
    a, b = result["data_api"], result["onchain"]
    m = result["matched_latency"]
    s = result["slippage"]
    lines = [
        "=" * 78,
        "SIGNAL SOURCE COMPARISON",
        "=" * 78,
        f"data-api : {a['path']}",
        f"           signals={a['signals']} copied={a['copied']} "
        f"notional=${a['notional']:.2f} fees=${a['fees']:.2f}",
        f"on-chain : {b['path']}",
        f"           signals={b['signals']} copied={b['copied']} "
        f"notional=${b['notional']:.2f} fees=${b['fees']:.2f}",
        f"data-api signals before on-chain start excluded: "
        f"{result['excluded_data_api_before_start']}",
        "",
        f"matched by (tx, side, token): {result['matched']}",
        f"lead of on-chain over data-api (s): median {fmt(m['lead_median'])}"
        f"  p90 {fmt(m['lead_p90'])}  max {fmt(m['lead_max'])}",
        f"copy latency (s)  data-api: median {fmt(m['data_api_median'])}"
        f"  p90 {fmt(m['data_api_p90'])}",
        f"                  on-chain: median {fmt(m['onchain_median'])}"
        f"  p90 {fmt(m['onchain_p90'])}",
        f"both copied: {result['both_copied']}",
        "adverse slippage vs target fill (c/share):",
        f"  data-api median {fmt(None if s['data_api_median'] is None else 100 * s['data_api_median'], 2)}"
        f"  mean {fmt(None if s['data_api_mean'] is None else 100 * s['data_api_mean'], 2)}",
        f"  on-chain median {fmt(None if s['onchain_median'] is None else 100 * s['onchain_median'], 2)}"
        f"  mean {fmt(None if s['onchain_mean'] is None else 100 * s['onchain_mean'], 2)}",
        f"  improvement median {fmt(None if s['improvement_median'] is None else 100 * s['improvement_median'], 2)}"
        f"  mean {fmt(None if s['improvement_mean'] is None else 100 * s['improvement_mean'], 2)}"
        "  (positive = on-chain better)",
        "",
        f"signals only in data-api: {result['only_data_api']}",
        f"signals only in on-chain: {result['only_onchain']}",
        f"data-api skip reasons: {a['reasons']}",
        f"on-chain skip reasons: {b['reasons']}",
    ]
    if result["only_data_api_samples"]:
        lines.append("only data-api samples: " + json.dumps(
            result["only_data_api_samples"][:5]))
    if result["only_onchain_samples"]:
        lines.append("only on-chain samples: " + json.dumps(
            result["only_onchain_samples"][:5]))
    if verbose:
        lines.extend(["", "PAIRS", "-" * 78])
        lines.append(
            f"{'tx':<14} {'side':<5} {'size':>10} {'api_lat':>8} "
            f"{'chain_lat':>9} {'api_px':>8} {'chain_px':>8} "
            f"{'api_slip':>9} {'chain_slip':>10}  reasons")
        for pair in result["pairs"]:
            lines.append(
                f"{str(pair['tx'])[:12]:<14} {str(pair['side']):<5} "
                f"{float(pair['size'] or 0):>10.4f} "
                f"{fmt(pair['api_latency'], 2):>8} "
                f"{fmt(pair['chain_latency'], 2):>9} "
                f"{fmt(pair['api_price'], 4):>8} "
                f"{fmt(pair['chain_price'], 4):>8} "
                f"{fmt(pair['api_slip'], 4):>9} "
                f"{fmt(pair['chain_slip'], 4):>10}  "
                f"{pair['api_reason'] or 'copied'}/{pair['chain_reason'] or 'copied'}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_api_db")
    parser.add_argument("onchain_db")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--since", type=int, default=None,
                        help="ignore data-api signals older than this epoch "
                             "(default: on-chain account creation time)")
    args = parser.parse_args(argv)
    result = compare(args.data_api_db, args.onchain_db,
                     verbose=args.verbose, since=args.since)
    print(format_report(result, verbose=args.verbose))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
