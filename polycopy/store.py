"""SQLite persistence for the paper trading account."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    starting_cash REAL NOT NULL,
    cash REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    fees_paid REAL NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS target_fills (
    dedupe_key TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    condition_id TEXT NOT NULL DEFAULT '',
    token_id TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL DEFAULT '',
    price REAL NOT NULL DEFAULT 0,
    size REAL NOT NULL DEFAULT 0,
    usd REAL NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT '',
    event_slug TEXT NOT NULL DEFAULT '',
    slug TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    raw TEXT NOT NULL DEFAULT '',
    processed_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_target_fills_target_ts ON target_fills(target, ts);

CREATE TABLE IF NOT EXISTS target_positions (
    target TEXT NOT NULL,
    condition_id TEXT NOT NULL DEFAULT '',
    token_id TEXT NOT NULL,
    shares REAL NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (target, token_id)
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    ts INTEGER NOT NULL,
    latency_seconds REAL,
    side TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT,
    price REAL,
    size REAL,
    usd REAL,
    title TEXT,
    event_slug TEXT,
    copied INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    our_shares REAL,
    our_price REAL,
    our_notional REAL,
    our_fee REAL,
    created_at INTEGER NOT NULL,
    UNIQUE(target, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_copied ON signals(copied);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT,
    title TEXT,
    event_slug TEXT,
    shares REAL NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    opened_at INTEGER,
    updated_at INTEGER,
    status TEXT NOT NULL DEFAULT 'OPEN',
    UNIQUE(target, condition_id, token_id)
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    target TEXT,
    signal_id INTEGER,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT,
    side TEXT NOT NULL,
    shares REAL NOT NULL,
    price REAL NOT NULL,
    notional REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'COPY',
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);

CREATE TABLE IF NOT EXISTS settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT,
    shares REAL NOT NULL,
    payout REAL NOT NULL,
    realized_pnl REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS marks (
    token_id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    bid REAL,
    ask REAL,
    mid REAL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(fills)")}
        if "realized_pnl" not in cols:
            self.conn.execute(
                "ALTER TABLE fills ADD COLUMN realized_pnl REAL NOT NULL DEFAULT 0"
            )

    # ------------------------------------------------------------------ account
    def ensure_account(self, starting_cash: float) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO account(id, starting_cash, cash, realized_pnl,"
            " fees_paid, created_at) VALUES (1, ?, ?, 0, 0, ?)",
            (starting_cash, starting_cash, int(time.time())),
        )
        self.conn.commit()

    def account(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("account not initialised; call ensure_account()")
        return dict(row)

    # ------------------------------------------------------------------- kv
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # --------------------------------------------------------- target ledger
    def insert_target_fill(self, dedupe_key: str, target: str,
                           row: dict[str, Any]) -> bool:
        price = _f(row.get("price"))
        size = _f(row.get("size"))
        usd = row.get("usdc_size")
        usd = float(usd) if usd is not None else size * price
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO target_fills(dedupe_key, target, ts, kind,"
            " condition_id, token_id, side, price, size, usd, outcome, event_slug,"
            " slug, title, raw, processed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                dedupe_key,
                target,
                int(row.get("timestamp") or 0),
                (row.get("type") or "").upper(),
                row.get("condition_id") or "",
                row.get("token_id") or "",
                (row.get("side") or "").upper(),
                price,
                size,
                usd,
                row.get("outcome") or "",
                row.get("event_slug") or "",
                row.get("slug") or "",
                row.get("title") or "",
                json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                int(time.time()),
            ),
        )
        return cur.rowcount > 0

    def target_shares(self, target: str, token_id: str) -> float:
        row = self.conn.execute(
            "SELECT shares FROM target_positions WHERE target = ? AND token_id = ?",
            (target, token_id),
        ).fetchone()
        return float(row["shares"]) if row else 0.0

    def add_target_shares(self, target: str, condition_id: str,
                          token_id: str, delta: float) -> None:
        if not token_id or abs(delta) < 1e-12:
            return
        now = int(time.time())
        row = self.conn.execute(
            "SELECT shares FROM target_positions WHERE target = ? AND token_id = ?",
            (target, token_id),
        ).fetchone()
        if row:
            new_shares = max(0.0, float(row["shares"]) + delta)
            self.conn.execute(
                "UPDATE target_positions SET shares = ?, updated_at = ?"
                " WHERE target = ? AND token_id = ?",
                (new_shares, now, target, token_id),
            )
        elif delta > 0:
            self.conn.execute(
                "INSERT INTO target_positions(target, condition_id, token_id, shares,"
                " updated_at) VALUES (?,?,?,?,?)",
                (target, condition_id, token_id, delta, now),
            )

    def set_target_shares(self, target: str, token_id: str, shares: float) -> None:
        if not token_id:
            return
        self.conn.execute(
            "UPDATE target_positions SET shares = ?, updated_at = ?"
            " WHERE target = ? AND token_id = ?",
            (max(0.0, shares), int(time.time()), target, token_id),
        )

    def target_tokens_for_condition(self, target: str, condition_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT token_id FROM target_positions WHERE target = ? AND condition_id = ?",
            (target, condition_id),
        ).fetchall()
        return [r["token_id"] for r in rows]

    # -------------------------------------------------------------- signals
    def record_signal(self, *, target: str, dedupe_key: str, ts: int,
                      latency_seconds: float, side: str, condition_id: str,
                      token_id: str, outcome: str, price: float, size: float,
                      usd: float, title: str, event_slug: str, copied: bool,
                      reason: str | None, our_shares: float | None = None,
                      our_price: float | None = None,
                      our_notional: float | None = None,
                      our_fee: float | None = None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO signals(target, dedupe_key, ts, latency_seconds,"
            " side, condition_id, token_id, outcome, price, size, usd, title,"
            " event_slug, copied, reason, our_shares, our_price, our_notional,"
            " our_fee, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                target, dedupe_key, ts, latency_seconds, side, condition_id,
                token_id, outcome, price, size, usd, title, event_slug,
                1 if copied else 0, reason, our_shares, our_price, our_notional,
                our_fee, int(time.time()),
            ),
        )

    # -------------------------------------------------------------- positions
    def position(self, target: str, condition_id: str,
                 token_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE target = ? AND condition_id = ?"
            " AND token_id = ?",
            (target, condition_id, token_id),
        ).fetchone()
        return dict(row) if row else None

    def open_positions(self, limit: int = 1000) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT p.*, m.bid AS mark_bid, m.ask AS mark_ask, m.mid AS mark_mid"
            " FROM positions p LEFT JOIN marks m ON m.token_id = p.token_id"
            " WHERE p.status = 'OPEN' ORDER BY p.shares * p.avg_cost DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def open_conditions(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT condition_id FROM positions WHERE status = 'OPEN'"
        ).fetchall()
        return [r["condition_id"] for r in rows]

    def positions_for_condition(self, condition_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE condition_id = ? AND status = 'OPEN'",
            (condition_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_open_positions(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM positions WHERE status = 'OPEN'"
        ).fetchone()
        return int(row["c"]) if row else 0

    def market_exposure(self, condition_id: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(shares * avg_cost), 0) AS v FROM positions"
            " WHERE condition_id = ? AND status = 'OPEN'",
            (condition_id,),
        ).fetchone()
        return float(row["v"]) if row else 0.0

    def apply_buy(self, *, ts: int, target: str, signal_id: int | None,
                  condition_id: str, token_id: str, outcome: str, title: str,
                  event_slug: str, shares: float, price: float,
                  fee: float) -> None:
        if shares <= 0:
            return
        notional = shares * price
        total_cost = notional + fee
        self.conn.execute(
            "UPDATE account SET cash = cash - ?, fees_paid = fees_paid + ? WHERE id = 1",
            (total_cost, fee),
        )
        row = self.conn.execute(
            "SELECT * FROM positions WHERE target = ? AND condition_id = ?"
            " AND token_id = ?",
            (target, condition_id, token_id),
        ).fetchone()
        if row and row["status"] == "OPEN":
            old_shares = float(row["shares"])
            new_shares = old_shares + shares
            new_avg = (float(row["avg_cost"]) * old_shares + total_cost) / new_shares
            self.conn.execute(
                "UPDATE positions SET shares = ?, avg_cost = ?, updated_at = ?"
                " WHERE id = ?",
                (new_shares, new_avg, ts, row["id"]),
            )
        elif row:
            self.conn.execute(
                "UPDATE positions SET status = 'OPEN', shares = ?, avg_cost = ?,"
                " outcome = ?, title = ?, event_slug = ?, opened_at = ?,"
                " updated_at = ? WHERE id = ?",
                (shares, total_cost / shares, outcome, title, event_slug, ts, ts,
                 row["id"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO positions(target, condition_id, token_id, outcome,"
                " title, event_slug, shares, avg_cost, realized_pnl, opened_at,"
                " updated_at, status) VALUES (?,?,?,?,?,?,?,?,0,?,?,'OPEN')",
                (target, condition_id, token_id, outcome, title, event_slug,
                 shares, total_cost / shares, ts, ts),
            )
        self.conn.execute(
            "INSERT INTO fills(ts, target, signal_id, condition_id, token_id,"
            " outcome, side, shares, price, notional, fee, kind)"
            " VALUES (?,?,?,?,?,?,'BUY',?,?,?,?,'COPY')",
            (ts, target, signal_id, condition_id, token_id, outcome, shares,
             price, notional, fee),
        )

    def apply_sell(self, *, ts: int, target: str, signal_id: int | None,
                   condition_id: str, token_id: str, outcome: str,
                   shares: float, price: float, fee: float) -> dict[str, float] | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE target = ? AND condition_id = ?"
            " AND token_id = ? AND status = 'OPEN'",
            (target, condition_id, token_id),
        ).fetchone()
        if row is None:
            return None
        held = float(row["shares"])
        sell_shares = min(shares, held)
        if sell_shares <= 0:
            return None
        if shares > 0:
            fee = fee * (sell_shares / shares)
        notional = sell_shares * price
        proceeds = notional - fee
        realized = proceeds - sell_shares * float(row["avg_cost"])
        remaining = held - sell_shares
        status = "CLOSED" if remaining <= 1e-9 else "OPEN"
        self.conn.execute(
            "UPDATE positions SET shares = ?, realized_pnl = realized_pnl + ?,"
            " status = ?, updated_at = ? WHERE id = ?",
            (max(0.0, remaining), realized, status, ts, row["id"]),
        )
        self.conn.execute(
            "UPDATE account SET cash = cash + ?, realized_pnl = realized_pnl + ?,"
            " fees_paid = fees_paid + ? WHERE id = 1",
            (proceeds, realized, fee),
        )
        self.conn.execute(
            "INSERT INTO fills(ts, target, signal_id, condition_id, token_id,"
            " outcome, side, shares, price, notional, fee, realized_pnl, kind)"
            " VALUES (?,?,?,?,?,?,'SELL',?,?,?,?,?,'COPY')",
            (ts, target, signal_id, condition_id, token_id, outcome, sell_shares,
             price, notional, fee, realized),
        )
        return {"shares": sell_shares, "price": price, "notional": notional,
                "fee": fee, "realized": realized}

    def settle_position(self, pos: dict[str, Any], payout_per_share: float,
                        ts: int) -> float:
        if pos.get("status") != "OPEN":
            return 0.0
        shares = float(pos["shares"])
        avg_cost = float(pos["avg_cost"])
        payout = shares * payout_per_share
        realized = payout - shares * avg_cost
        self.conn.execute(
            "UPDATE positions SET shares = 0, status = 'CLOSED',"
            " realized_pnl = realized_pnl + ?, updated_at = ? WHERE id = ?",
            (realized, ts, pos["id"]),
        )
        self.conn.execute(
            "UPDATE account SET cash = cash + ?, realized_pnl = realized_pnl + ?"
            " WHERE id = 1",
            (payout, realized),
        )
        self.conn.execute(
            "INSERT INTO settlements(ts, condition_id, token_id, outcome, shares,"
            " payout, realized_pnl) VALUES (?,?,?,?,?,?,?)",
            (ts, pos["condition_id"], pos["token_id"], pos.get("outcome"),
             shares, payout, realized),
        )
        return realized

    # ------------------------------------------------------------------ marks
    def set_mark(self, token_id: str, bid: float | None, ask: float | None,
                 mid: float | None, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO marks(token_id, ts, bid, ask, mid) VALUES (?,?,?,?,?)"
            " ON CONFLICT(token_id) DO UPDATE SET ts = excluded.ts,"
            " bid = excluded.bid, ask = excluded.ask, mid = excluded.mid",
            (token_id, ts, bid, ask, mid),
        )

    # --------------------------------------------------------------- reporting
    def signal_summary(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT copied, reason, COUNT(*) AS n, COALESCE(SUM(our_notional),0)"
            " AS notional FROM signals GROUP BY copied, reason ORDER BY n DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def signal_counts(self) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(copied),0) AS copied FROM signals"
        ).fetchone()
        return int(row["total"]), int(row["copied"])

    def recent_fills(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def latency_stats(self) -> dict[str, float] | None:
        rows = self.conn.execute(
            "SELECT latency_seconds FROM signals WHERE copied = 1"
            " AND latency_seconds IS NOT NULL ORDER BY latency_seconds"
        ).fetchall()
        vals = [float(r["latency_seconds"]) for r in rows]
        if not vals:
            return None
        def q(p: float) -> float:
            return vals[min(len(vals) - 1, max(0, int(round(p * (len(vals) - 1)))))]
        return {"n": len(vals), "median": q(0.5), "p90": q(0.9),
                "max": vals[-1]}

    def all_positions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM positions ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def all_fills(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM fills ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def all_signals(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM signals ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
