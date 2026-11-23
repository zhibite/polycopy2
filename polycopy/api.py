"""Thin clients for Polymarket's public data, CLOB and Gamma APIs."""
from __future__ import annotations

from typing import Any

from .http import HttpClient

DATA_API = "https://data-api.polymarket.com/v2"
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


class DataApi:
    """Public wallet activity feed."""

    def __init__(self, http: HttpClient):
        self.http = http

    def activity(self, user: str, start_ts: int, limit: int = 500,
                 max_pages: int = 3) -> list[dict[str, Any]]:
        """Return activity rows (newest first) with timestamp >= start_ts."""
        params = {
            "user": user,
            "start": int(start_ts),
            "limit": limit,
            "sort_by": "TIMESTAMP",
            "sort_direction": "DESC",
        }
        rows: list[dict[str, Any]] = []
        cursor = None
        for _ in range(max(1, max_pages)):
            page = dict(params)
            if cursor:
                page["cursor"] = cursor
            body = self.http.get_json(DATA_API + "/activity", page)
            if not isinstance(body, dict):
                break
            data = body.get("data") or []
            rows.extend(data)
            pag = body.get("pagination") or {}
            cursor = pag.get("next_cursor")
            if not pag.get("has_more") or not cursor:
                break
            oldest = int(data[-1].get("timestamp") or 0) if data else 0
            if oldest and oldest < start_ts:
                break
        return rows

    def open_positions(self, user: str, limit: int = 500,
                       max_pages: int = 10) -> list[dict[str, Any]]:
        params = {"user": user, "status": "OPEN", "limit": limit}
        rows: list[dict[str, Any]] = []
        cursor = None
        for _ in range(max(1, max_pages)):
            page = dict(params)
            if cursor:
                page["cursor"] = cursor
            body = self.http.get_json(DATA_API + "/positions", page)
            if not isinstance(body, dict):
                break
            data = body.get("data") or []
            rows.extend(data)
            pag = body.get("pagination") or {}
            cursor = pag.get("next_cursor")
            if not pag.get("has_more") or not cursor:
                break
        return rows


class ClobApi:
    """Order book and fee information."""

    def __init__(self, http: HttpClient):
        self.http = http

    def book(self, token_id: str) -> dict[str, Any] | None:
        body = self.http.get_json(CLOB_API + "/book", {"token_id": token_id})
        return body if isinstance(body, dict) else None

    def fee_rate(self, token_id: str) -> float | None:
        """Return the market's fee rate, or None.

        The API reports base_fee=1000 for the markets sampled so far, which
        corresponds to the documented 1% (0.01) rate used for general crypto
        markets. BTC 5m/15m up-down markets use 0.05 — set that per target in
        the config rather than trusting this endpoint.
        """
        body = self.http.get_json(CLOB_API + "/fee-rate", {"token_id": token_id})
        if not isinstance(body, dict):
            return None
        base = body.get("base_fee")
        if base is None:
            return None
        try:
            return float(base) / 100_000.0
        except (TypeError, ValueError):
            return None


class GammaApi:
    """Market metadata and resolution status."""

    def __init__(self, http: HttpClient):
        self.http = http

    def market_by_condition(self, condition_id: str) -> dict[str, Any] | None:
        body = self.http.get_json(GAMMA_API + "/markets", {"condition_ids": condition_id})
        if isinstance(body, list) and body:
            return body[0]
        return None
