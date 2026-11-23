"""Pure rule helpers: dedupe keys, activity normalisation, buy gates, sizing."""
from __future__ import annotations

from typing import Any

from .config import TargetConfig


def dedupe_key(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("transaction_hash") or ""),
            str(row.get("type") or "").upper(),
            str(row.get("condition_id") or ""),
            str(row.get("token_id") or ""),
            str(row.get("side") or "").upper(),
            f"{to_float(row.get('size')):.8f}",
            f"{to_float(row.get('price')):.8f}",
            str(int(row.get("timestamp") or 0)),
        ]
    )


def target_usd(row: dict[str, Any]) -> float:
    value = row.get("usdc_size")
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    return to_float(row.get("size")) * to_float(row.get("price"))


def buy_skip_reason(t: TargetConfig, row: dict[str, Any], *, now: int,
                    stale_seconds: int, open_positions: int,
                    has_position: bool, market_usd: float) -> str | None:
    if not t.enabled:
        return "disabled"
    ts = int(row.get("timestamp") or 0)
    if now - ts > stale_seconds:
        return "stale"
    price = to_float(row.get("price"))
    usd = target_usd(row)
    event_slug = str(row.get("event_slug") or "").lower()
    slug = str(row.get("slug") or "").lower()

    if price < t.buy_price_min or price > t.buy_price_max:
        return "price_out_of_range"
    if usd < t.min_target_usd:
        return "target_too_small"
    if usd > t.max_target_usd:
        return "target_too_large"
    for needle in t.exclude_event_substrings:
        if needle and needle.lower() in event_slug:
            return "event_excluded"
    for needle in t.exclude_slug_substrings:
        if needle and needle.lower() in slug:
            return "slug_excluded"
    if not has_position and open_positions >= t.max_open_positions:
        return "max_open_positions"
    if market_usd >= t.max_market_usd:
        return "max_market_exposure"
    return None


def desired_notional(t: TargetConfig, usd: float) -> float:
    if t.sizing_mode == "fixed":
        desired = t.fixed_notional_usd
    elif t.sizing_mode == "scale":
        desired = usd * t.scale_factor
    else:
        raise ValueError(f"unknown sizing_mode {t.sizing_mode!r}")
    return max(0.0, min(desired, t.max_notional_usd))


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
