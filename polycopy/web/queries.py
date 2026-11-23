"""Read-only queries against the paper-trading SQLite database."""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BJ = timezone(timedelta(hours=8))

PROJECT_DIR = Path(
    os.environ.get("POLY_PROJECT_DIR", Path(__file__).resolve().parents[2])
)
DB_PATH = Path(os.environ.get("POLY_DB_PATH", PROJECT_DIR / "data" / "paper_f705.db"))
CONFIG_PATH = Path(
    os.environ.get("POLY_CONFIG_PATH", PROJECT_DIR / "configs" / "paper_f705.toml")
)

PAGE_DEFAULT = 50


def connect() -> sqlite3.Connection | None:
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error:
        return None


def close(conn: sqlite3.Connection | None) -> None:
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _all(conn: sqlite3.Connection | None, sql: str,
         params: tuple | list = ()) -> list[dict[str, Any]]:
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    except sqlite3.Error:
        return []


def _one(conn: sqlite3.Connection | None, sql: str,
         params: tuple | list = ()) -> dict[str, Any] | None:
    rows = _all(conn, sql, params)
    return rows[0] if rows else None


def _scalar(conn: sqlite3.Connection | None, sql: str,
            params: tuple | list = (), default: float = 0.0) -> float:
    row = _one(conn, sql, params)
    if not row:
        return default
    value = next(iter(row.values()))
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- primitives

def account(conn: sqlite3.Connection | None) -> dict[str, Any]:
    row = _one(conn, "SELECT * FROM account WHERE id = 1")
    return row or {
        "starting_cash": 0.0, "cash": 0.0, "realized_pnl": 0.0,
        "fees_paid": 0.0, "created_at": None,
    }


def kv_int(conn: sqlite3.Connection | None, key: str) -> int | None:
    row = _one(conn, "SELECT value FROM kv WHERE key = ?", (key,))
    if not row:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def open_positions(conn: sqlite3.Connection | None,
                   limit: int = 500) -> list[dict[str, Any]]:
    return _all(
        conn,
        "SELECT p.*, m.bid AS mark_bid, m.ask AS mark_ask, m.mid AS mark_mid"
        " FROM positions p LEFT JOIN marks m ON m.token_id = p.token_id"
        " WHERE p.status = 'OPEN'"
        " ORDER BY p.shares * p.avg_cost DESC LIMIT ?",
        (limit,),
    )


def recent_fills(conn: sqlite3.Connection | None,
                 limit: int = 15) -> list[dict[str, Any]]:
    return _all(
        conn,
        "SELECT f.*, p.title AS title, p.event_slug AS event_slug"
        " FROM fills f LEFT JOIN positions p ON p.target = f.target"
        " AND p.condition_id = f.condition_id AND p.token_id = f.token_id"
        " ORDER BY f.ts DESC, f.id DESC LIMIT ?",
        (limit,),
    )


def recent_settlements(conn: sqlite3.Connection | None,
                       limit: int = 20) -> list[dict[str, Any]]:
    return _all(
        conn,
        "SELECT s.*, p.title AS title, p.event_slug AS event_slug"
        " FROM settlements s LEFT JOIN positions p"
        " ON p.condition_id = s.condition_id AND p.token_id = s.token_id"
        " ORDER BY s.ts DESC, s.id DESC LIMIT ?",
        (limit,),
    )


# ------------------------------------------------------------------ summary

def summary(conn: sqlite3.Connection | None) -> dict[str, Any]:
    acc = account(conn)
    opens = open_positions(conn, limit=10_000)
    market_value = 0.0
    unrealized = 0.0
    cost = 0.0
    for pos in opens:
        shares = float(pos.get("shares") or 0)
        avg = float(pos.get("avg_cost") or 0)
        mark = pos.get("mark_mid")
        mark = float(mark) if mark is not None else avg
        market_value += shares * mark
        cost += shares * avg
        unrealized += shares * (mark - avg)

    cash = float(acc.get("cash") or 0)
    starting = float(acc.get("starting_cash") or 0)
    equity = cash + market_value
    total_pnl = equity - starting

    today_start = int(
        datetime.now(BJ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    today_realized = _scalar(
        conn, "SELECT COALESCE(SUM(realized_pnl),0) FROM fills WHERE ts >= ?",
        (today_start,),
    ) + _scalar(
        conn, "SELECT COALESCE(SUM(realized_pnl),0) FROM settlements WHERE ts >= ?",
        (today_start,),
    )
    today_fills = int(_scalar(
        conn, "SELECT COUNT(*) FROM fills WHERE ts >= ?", (today_start,)
    ))

    signals_total = int(_scalar(conn, "SELECT COUNT(*) FROM signals"))
    signals_copied = int(_scalar(conn, "SELECT COUNT(*) FROM signals WHERE copied = 1"))

    closed = _all(
        conn,
        "SELECT realized_pnl FROM positions WHERE status = 'CLOSED'",
    )
    pnls = [float(r.get("realized_pnl") or 0) for r in closed]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    win_sum = sum(wins)
    loss_sum = sum(losses)
    profit_factor = (win_sum / abs(loss_sum)) if loss_sum < 0 else None

    heartbeat = kv_int(conn, "bot_heartbeat")
    now = int(time.time())
    bot_online = bool(heartbeat and now - heartbeat <= 90)

    last_target_fill = int(_scalar(
        conn, "SELECT COALESCE(MAX(ts),0) FROM target_fills"
    ) or 0)

    return {
        "starting_cash": starting,
        "cash": cash,
        "market_value": market_value,
        "cost": cost,
        "equity": equity,
        "unrealized": unrealized,
        "realized": float(acc.get("realized_pnl") or 0),
        "fees_paid": float(acc.get("fees_paid") or 0),
        "total_pnl": total_pnl,
        "total_return": (total_pnl / starting) if starting else None,
        "today_realized": today_realized,
        "today_fills": today_fills,
        "open_positions": len(opens),
        "signals_total": signals_total,
        "signals_copied": signals_copied,
        "copy_rate": (signals_copied / signals_total) if signals_total else None,
        "closed_positions": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(pnls)) if pnls else None,
        "profit_factor": profit_factor,
        "bot_heartbeat": heartbeat,
        "bot_online": bot_online,
        "last_target_fill": last_target_fill or None,
    }


def daily_pnl(conn: sqlite3.Connection | None, days: int = 14) -> list[dict[str, Any]]:
    return _all(
        conn,
        "SELECT d, ROUND(SUM(pnl), 2) AS pnl, SUM(n) AS n FROM ("
        "  SELECT date(ts, 'unixepoch', '+8 hours') AS d, realized_pnl AS pnl,"
        "         1 AS n FROM fills WHERE realized_pnl IS NOT NULL"
        "  UNION ALL"
        "  SELECT date(ts, 'unixepoch', '+8 hours') AS d, realized_pnl AS pnl,"
        "         1 AS n FROM settlements"
        ") GROUP BY d ORDER BY d DESC LIMIT ?",
        (days,),
    )


# --------------------------------------------------------------- pagination

def _page_bounds(page: int, per_page: int) -> tuple[int, int]:
    page = max(1, int(page or 1))
    per_page = max(1, min(500, int(per_page or PAGE_DEFAULT)))
    return (page - 1) * per_page, per_page


def fills_page(conn: sqlite3.Connection | None, *, side: str = "",
               target: str = "", start_ts: int | None = None,
               end_ts: int | None = None, page: int = 1,
               per_page: int = PAGE_DEFAULT) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if side in ("BUY", "SELL"):
        where.append("f.side = ?")
        params.append(side)
    if target:
        where.append("f.target = ?")
        params.append(target)
    if start_ts:
        where.append("f.ts >= ?")
        params.append(int(start_ts))
    if end_ts:
        where.append("f.ts < ?")
        params.append(int(end_ts))
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    offset, limit = _page_bounds(page, per_page)
    total = int(_scalar(conn, "SELECT COUNT(*) FROM fills f" + clause, params))
    rows = _all(
        conn,
        "SELECT f.*, p.title AS title, p.event_slug AS event_slug"
        " FROM fills f LEFT JOIN positions p ON p.target = f.target"
        " AND p.condition_id = f.condition_id AND p.token_id = f.token_id"
        + clause + " ORDER BY f.ts DESC, f.id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    sums = _one(
        conn,
        "SELECT COALESCE(SUM(f.notional),0) AS notional,"
        " COALESCE(SUM(f.fee),0) AS fee,"
        " COALESCE(SUM(f.realized_pnl),0) AS realized,"
        " COALESCE(SUM(CASE WHEN f.side='BUY' THEN f.notional END),0) AS buy_notional,"
        " COALESCE(SUM(CASE WHEN f.side='SELL' THEN f.notional END),0) AS sell_notional"
        " FROM fills f" + clause,
        params,
    ) or {}
    return {
        "rows": rows, "total": total, "page": page, "per_page": limit,
        "offset": offset, "sums": sums,
    }


def positions_page(conn: sqlite3.Connection | None, *, status: str = "open",
                   page: int = 1, per_page: int = PAGE_DEFAULT) -> dict[str, Any]:
    status = status if status in ("open", "closed", "all") else "open"
    where = ""
    if status == "open":
        where = " WHERE p.status = 'OPEN'"
    elif status == "closed":
        where = " WHERE p.status = 'CLOSED'"
    offset, limit = _page_bounds(page, per_page)
    total = int(_scalar(conn, "SELECT COUNT(*) FROM positions p" + where))
    rows = _all(
        conn,
        "SELECT p.*, m.bid AS mark_bid, m.ask AS mark_ask, m.mid AS mark_mid"
        " FROM positions p LEFT JOIN marks m ON m.token_id = p.token_id"
        + where + " ORDER BY p.updated_at DESC, p.id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    totals = _one(
        conn,
        "SELECT COUNT(*) AS n, COALESCE(SUM(p.shares * p.avg_cost),0) AS cost,"
        " COALESCE(SUM(p.shares * COALESCE(m.mid, p.avg_cost)),0) AS value,"
        " COALESCE(SUM(p.shares * (COALESCE(m.mid, p.avg_cost) - p.avg_cost)),0)"
        " AS unrealized, COALESCE(SUM(p.realized_pnl),0) AS realized"
        " FROM positions p LEFT JOIN marks m ON m.token_id = p.token_id" + where,
    ) or {}
    return {
        "rows": rows, "total": total, "page": page, "per_page": limit,
        "offset": offset, "status": status, "totals": totals,
    }


def signals_page(conn: sqlite3.Connection | None, *, copied: str = "",
                 reason: str = "", page: int = 1,
                 per_page: int = 100) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if copied in ("1", "0"):
        where.append("copied = ?")
        params.append(int(copied))
    if reason:
        where.append("reason = ?")
        params.append(reason)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    offset, limit = _page_bounds(page, per_page)
    total = int(_scalar(conn, "SELECT COUNT(*) FROM signals" + clause, params))
    rows = _all(
        conn,
        "SELECT * FROM signals" + clause
        + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    return {
        "rows": rows, "total": total, "page": page, "per_page": limit,
        "offset": offset,
    }


def signal_summary(conn: sqlite3.Connection | None) -> list[dict[str, Any]]:
    return _all(
        conn,
        "SELECT copied, reason, COUNT(*) AS n,"
        " COALESCE(SUM(our_notional),0) AS notional"
        " FROM signals GROUP BY copied, reason ORDER BY n DESC",
    )


def target_summary(conn: sqlite3.Connection | None) -> list[dict[str, Any]]:
    return _all(
        conn,
        "SELECT target, COUNT(*) AS fills, MAX(ts) AS last_ts"
        " FROM target_fills GROUP BY target ORDER BY fills DESC",
    )
