"""Compare decoded on-chain OrderFilled events with data-api trades.

The PoC takes a sample of known data-api fills for a wallet, finds the
matching V2 OrderFilled logs, decodes price / size / side / cash flow, and
reports field-by-field differences.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable

from .api import ClobApi, DataApi
from .http import HttpClient
from .onchain import OrderFill, RpcClient, decode_order_filled

SIZE_TOL = 1e-6                # CTF shares carry 6 decimals
PRICE_TOL = 1e-6               # data-api prints up to 10 decimals
USD_TOL = 2e-5                 # data-api cash values are rounded to 5 decimals


@dataclass
class RowCheck:
    row: dict[str, Any]
    fill: OrderFill | None
    block_ts: int | None
    condition_for_token: str | None
    diffs: dict[str, float | None]
    ok: bool
    error: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "tx": self.row.get("transaction_hash"),
            "side": self.row.get("side"),
            "size": self.row.get("size"),
            "price": self.row.get("price"),
            "usdc_size": self.row.get("usdc_size"),
            "timestamp": self.row.get("timestamp"),
            "token_id": self.row.get("token_id"),
            "condition_id": self.row.get("condition_id"),
            "matched_log_index": self.fill.log_index if self.fill else None,
            "block_number": self.fill.block_number if self.fill else None,
            "block_ts": self.block_ts,
            "decoded": {
                "side": self.fill.side,
                "size": self.fill.shares,
                "price": self.fill.price,
                "usdc_size": self.fill.net_usd,
                "gross_usd": self.fill.gross_usd,
                "fee_usd": self.fill.fee_usd,
            } if self.fill else None,
            "diffs": self.diffs,
            "condition_matches": (
                None if self.condition_for_token is None
                else self.condition_for_token == str(self.row.get("condition_id") or "")
            ),
            "ok": self.ok,
            "error": self.error,
        }


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _fetch_receipts(rpc: RpcClient,
                    txs: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch receipts in JSON-RPC batches; tolerate individual failures."""
    receipts: dict[str, dict[str, Any]] = {}
    # drpc accepts batches of 10 but rejects 20; publicnode handles either.
    for chunk in _chunks(txs, 10):
        results = rpc.batch([("eth_getTransactionReceipt", [tx]) for tx in chunk])
        for tx, result in zip(chunk, results):
            if isinstance(result, dict):
                receipts[tx.lower()] = result
    return receipts


def _block_timestamps(rpc: RpcClient, blocks: Iterable[int]) -> dict[int, int]:
    numbers = sorted({int(b) for b in blocks if b})
    stamps: dict[int, int] = {}
    for chunk in _chunks(numbers, 10):
        results = rpc.batch([
            ("eth_getBlockByNumber", [hex(number), False]) for number in chunk
        ])
        for number, block in zip(chunk, results):
            if not isinstance(block, dict):
                continue
            try:
                stamps[number] = int(str(block.get("timestamp")), 16)
            except (TypeError, ValueError):
                continue
    return stamps


def _condition_for_token(clob: ClobApi, token_id: str,
                         cache: dict[str, str | None]) -> str | None:
    if token_id in cache:
        return cache[token_id]
    try:
        body = clob.market_by_token(token_id)
    except Exception:
        body = None
    cond = str((body or {}).get("condition_id") or "") or None
    cache[token_id] = cond
    return cond


def verify_wallet(http: HttpClient, rpc: RpcClient, wallet: str, *,
                  count: int = 30, lookback: int = 86_400,
                  logger: logging.Logger | None = None) -> dict[str, Any]:
    """Verify up to ``count`` recent data-api trades for one wallet."""
    log = logger or logging.getLogger("polycopy.onchain.verify")
    wallet = wallet.lower()
    api = DataApi(http)
    clob = ClobApi(http)
    now = int(time.time())
    rows = api.activity(wallet, start_ts=now - max(0, lookback),
                        limit=500, max_pages=10)
    trades = [row for row in rows
              if (row.get("type") or "").upper() == "TRADE"
              and row.get("transaction_hash")]
    trades.sort(key=lambda row: int(row.get("timestamp") or 0), reverse=True)
    trades = trades[:max(1, count)]
    txs = sorted({str(row["transaction_hash"]).lower() for row in trades})
    log.info("wallet=%s data-api trades=%d txs=%d", wallet, len(trades), len(txs))

    receipts = _fetch_receipts(rpc, txs)
    events_by_tx: dict[str, list[OrderFill]] = {}
    all_events: list[OrderFill] = []
    for tx, receipt in receipts.items():
        fills: list[OrderFill] = []
        for raw in receipt.get("logs") or []:
            fill = decode_order_filled(raw)
            if fill is None or not fill.is_mine(wallet):
                continue
            fills.append(fill)
            all_events.append(fill)
        events_by_tx[tx] = fills

    stamps = _block_timestamps(rpc, (fill.block_number for fill in all_events))
    for tx, fills in events_by_tx.items():
        events_by_tx[tx] = [
            fill if fill.block_ts is not None
            else OrderFill(**{**fill.__dict__,
                              "block_ts": stamps.get(fill.block_number)})
            for fill in fills
        ]

    condition_cache: dict[str, str | None] = {}
    checks: list[RowCheck] = []
    used: set[tuple[str, int]] = set()

    for row in trades:
        tx = str(row.get("transaction_hash") or "").lower()
        candidates = [fill for fill in events_by_tx.get(tx, [])
                      if (fill.tx_hash, fill.log_index) not in used]
        if not candidates:
            checks.append(RowCheck(
                row=row, fill=None, block_ts=None,
                condition_for_token=None, diffs={}, ok=False,
                error="no matching own-order OrderFilled in tx",
            ))
            continue

        token = str(row.get("token_id") or "")
        side = str(row.get("side") or "").upper()
        same = [fill for fill in candidates
                if fill.token_id == token and fill.side == side]
        pool = same or candidates
        fill = min(
            pool,
            key=lambda f: (abs(f.shares - float(row.get("size") or 0))
                           + abs(f.price - float(row.get("price") or 0)) * 1000),
        )
        used.add((fill.tx_hash, fill.log_index))

        size = float(row.get("size") or 0)
        price = float(row.get("price") or 0)
        usdc = row.get("usdc_size")
        usd = float(usdc) if usdc is not None else size * price
        block_ts = stamps.get(fill.block_number, fill.block_ts)
        cond = _condition_for_token(clob, token, condition_cache)
        diffs = {
            "size": fill.shares - size,
            "price": fill.price - price,
            "usdc_size": fill.net_usd - usd,
            "timestamp": (block_ts - int(row.get("timestamp") or 0)
                          if block_ts is not None else None),
        }
        token_match = fill.token_id == token
        side_match = fill.side == side
        size_match = abs(diffs["size"]) <= SIZE_TOL
        price_match = abs(diffs["price"]) <= PRICE_TOL
        usd_match = abs(diffs["usdc_size"]) <= USD_TOL
        ts_match = diffs["timestamp"] == 0 if block_ts is not None else None
        condition_ok = None if cond is None else cond == str(
            row.get("condition_id") or "")
        reasons: list[str] = []
        if not token_match:
            reasons.append("token")
        if not side_match:
            reasons.append("side")
        if not size_match:
            reasons.append("size")
        if not price_match:
            reasons.append("price")
        if not usd_match:
            reasons.append("usdc_size")
        if ts_match is False:
            reasons.append("timestamp")
        if condition_ok is False:
            reasons.append("condition")
        checks.append(RowCheck(
            row=row, fill=fill, block_ts=block_ts,
            condition_for_token=cond, diffs=diffs, ok=not reasons,
            error=",".join(reasons) or None,
        ))

    unmatched_events = [
        fill for fill in all_events
        if (fill.tx_hash, fill.log_index) not in used
    ]
    passed = sum(1 for check in checks if check.ok)
    matched = sum(1 for check in checks if check.fill is not None)

    def max_abs(key: str) -> float | None:
        values = [abs(check.diffs[key]) for check in checks
                  if check.diffs.get(key) is not None]
        return max(values) if values else None

    result = {
        "wallet": wallet,
        "generated_at": now,
        "lookback_seconds": lookback,
        "requested": count,
        "data_api_trades": len(trades),
        "transactions": len(txs),
        "receipts": len(receipts),
        "own_order_events": len(all_events),
        "matched": matched,
        "passed": passed,
        "failed": len(checks) - passed,
        "max_abs_size_diff": max_abs("size"),
        "max_abs_price_diff": max_abs("price"),
        "max_abs_usdc_diff": max_abs("usdc_size"),
        "timestamp_mismatches": sum(
            1 for check in checks if check.diffs.get("timestamp") not in (None, 0)),
        "condition_mismatches": sum(
            1 for check in checks
            if check.condition_for_token is not None
            and check.condition_for_token != str(check.row.get("condition_id") or "")),
        "unmatched_rows": sum(1 for check in checks if check.fill is None),
        "unmatched_events": len(unmatched_events),
        "checks": [check.as_json() for check in checks],
        "unmatched_event_details": [
            {
                "tx": fill.tx_hash,
                "log_index": fill.log_index,
                "side": fill.side,
                "size": fill.shares,
                "price": fill.price,
                "usdc_size": fill.net_usd,
                "token_id": fill.token_id,
            }
            for fill in unmatched_events
        ],
    }
    return result


def format_verification(result: dict[str, Any], *, show_rows: bool = True) -> str:
    lines: list[str] = []
    lines.append("=" * 92)
    lines.append("ON-CHAIN ORDERFILLED VERIFICATION  wallet=" + result["wallet"])
    lines.append("=" * 92)
    lines.append(
        f"data-api trades: {result['data_api_trades']}  txs: {result['transactions']}"
        f"  receipts: {result['receipts']}  own-order events: {result['own_order_events']}"
    )
    lines.append(
        f"matched: {result['matched']}  passed: {result['passed']}"
        f"  failed: {result['failed']}  unmatched rows: {result['unmatched_rows']}"
        f"  unmatched events: {result['unmatched_events']}"
    )
    lines.append(
        "max |diff|: size %.8f  price %.8f  usdc %.8f  timestamp mismatches %d"
        "  condition mismatches %d" % (
            result["max_abs_size_diff"] or 0.0,
            result["max_abs_price_diff"] or 0.0,
            result["max_abs_usdc_diff"] or 0.0,
            result["timestamp_mismatches"],
            result["condition_mismatches"],
        )
    )
    if not show_rows:
        return "\n".join(lines)

    lines.append("")
    header = (f"{'tx':<14} {'side':<5} {'size':>12} {'price':>12} "
              f"{'usdc':>12}  {'d_size':>9} {'d_price':>10} {'d_usdc':>9} "
              f"{'d_ts':>5} {'cond':>4}  result")
    lines.append(header)
    lines.append("-" * len(header))
    for check in result["checks"]:
        row = check
        tx = str(row.get("tx") or "")[:12]
        diffs = row.get("diffs") or {}
        cond = row.get("condition_matches")
        cond_text = "-" if cond is None else ("ok" if cond else "BAD")
        status = "PASS" if row.get("ok") else ("FAIL " + str(row.get("error")))
        lines.append(
            f"{tx:<14} {str(row.get('side')):<5} {float(row.get('size') or 0):>12.6f} "
            f"{float(row.get('price') or 0):>12.8f} "
            f"{float(row.get('usdc_size') or 0):>12.6f}  "
            f"{_num(diffs.get('size')):>9} {_num(diffs.get('price')):>10} "
            f"{_num(diffs.get('usdc_size')):>9} {_num(diffs.get('timestamp')):>5} "
            f"{cond_text:>4}  {status}"
        )
    if result["unmatched_event_details"]:
        lines.append("")
        lines.append("unmatched own-order events:")
        for item in result["unmatched_event_details"]:
            lines.append(
                f"  {item['tx'][:14]} log={item['log_index']} {item['side']} "
                f"size={item['size']:.6f} price={item['price']:.8f} "
                f"usdc={item['usdc_size']:.6f} token={item['token_id'][:16]}.."
            )
    return "\n".join(lines)


def _num(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.2e}"
