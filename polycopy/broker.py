"""Order-book walking and taker-fee model for paper fills."""
from __future__ import annotations

from dataclasses import dataclass

from .config import PaperConfig


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class Execution:
    shares: float
    price: float


def parse_book(payload: dict) -> tuple[list[Level], list[Level]]:
    def levels(key: str) -> list[Level]:
        out: list[Level] = []
        for item in payload.get(key) or []:
            try:
                out.append(Level(float(item["price"]), float(item["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        return out
    return levels("bids"), levels("asks")


def simulate_buy(book: tuple[list[Level], list[Level]], shares: float,
                 signal_price: float, cfg: PaperConfig) -> tuple[Execution | None, str | None]:
    """Take liquidity from asks, walking levels up to signal_price + max drift."""
    _, asks = book
    cap = signal_price + cfg.max_buy_drift
    eligible = sorted((lv for lv in asks if lv.price <= cap + 1e-12),
                      key=lambda lv: lv.price)
    if not eligible:
        return None, "drift"
    filled, cost = _walk(eligible, shares)
    if filled <= 0:
        return None, "no_liquidity"
    if filled < shares * cfg.min_fill_ratio:
        return None, "insufficient_depth"
    avg = cost / filled + cfg.extra_slippage
    if avg > cap + 1e-9:
        return None, "drift"
    return Execution(filled, avg), None


def simulate_sell(book: tuple[list[Level], list[Level]], shares: float,
                  signal_price: float, cfg: PaperConfig) -> tuple[Execution | None, str | None]:
    """Hit bids, walking levels down to signal_price - max drift."""
    bids, _ = book
    floor = signal_price - cfg.max_sell_drift
    eligible = sorted((lv for lv in bids if lv.price >= floor - 1e-12),
                      key=lambda lv: -lv.price)
    if not eligible:
        return None, "drift"
    filled, proceeds = _walk(eligible, shares)
    if filled <= 0:
        return None, "no_liquidity"
    if filled < shares * cfg.min_fill_ratio:
        return None, "insufficient_depth"
    avg = proceeds / filled - cfg.extra_slippage
    if avg < floor - 1e-9:
        return None, "drift"
    return Execution(filled, avg), None


def _walk(levels: list[Level], shares: float) -> tuple[float, float]:
    filled = 0.0
    value = 0.0
    for lv in levels:
        if filled >= shares - 1e-12:
            break
        take = min(shares - filled, lv.size)
        if take <= 0:
            continue
        filled += take
        value += take * lv.price
    return filled, value


def taker_fee(shares: float, price: float, fee_rate: float) -> float:
    """Polymarket taker fee: shares * feeRate * p * (1 - p)."""
    return max(0.0, shares * price * (1.0 - price) * fee_rate)


def best_bid_ask(book: tuple[list[Level], list[Level]]) -> tuple[float | None, float | None]:
    bids, asks = book
    bid = max((lv.price for lv in bids), default=None)
    ask = min((lv.price for lv in asks), default=None)
    return bid, ask
