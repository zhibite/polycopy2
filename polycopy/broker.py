"""Order-book walking and taker-fee model for paper fills."""
from __future__ import annotations

from dataclasses import dataclass

from .config import PaperConfig


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass(frozen=True)
class Book:
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    tick_size: float = 0.001


@dataclass(frozen=True)
class Execution:
    shares: float
    price: float


def parse_book(payload: dict) -> Book:
    def levels(key: str) -> tuple[Level, ...]:
        out: list[Level] = []
        for item in payload.get(key) or []:
            try:
                out.append(Level(float(item["price"]), float(item["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        return tuple(out)
    try:
        tick = float(payload.get("tick_size") or 0.001)
    except (TypeError, ValueError):
        tick = 0.001
    if tick <= 0:
        tick = 0.001
    return Book(bids=levels("bids"), asks=levels("asks"), tick_size=tick)


def buy_cap(signal_price: float, cfg: PaperConfig, tick_size: float) -> float:
    """Max price we accept: absolute drift cap AND relative drift cap.

    The relative cap stops low-priced tokens (e.g. 0.003) from filling at
    0.01–0.03, which the absolute 3c cap would otherwise allow. One tick of
    headroom keeps very cheap tokens tradable.
    """
    absolute = signal_price + cfg.max_buy_drift
    relative = signal_price * (1.0 + cfg.max_buy_drift_pct)
    return min(absolute, max(relative, signal_price + max(tick_size, 0.0)))


def sell_floor(signal_price: float, cfg: PaperConfig, tick_size: float) -> float:
    absolute = signal_price - cfg.max_sell_drift
    relative = signal_price * (1.0 - cfg.max_sell_drift_pct)
    return max(absolute, min(relative, signal_price - max(tick_size, 0.0)))


def simulate_buy(book: Book, shares: float, signal_price: float,
                 cfg: PaperConfig) -> tuple[Execution | None, str | None]:
    """Take liquidity from asks, walking levels up to the allowed cap."""
    cap = buy_cap(signal_price, cfg, book.tick_size)
    eligible = sorted((lv for lv in book.asks if lv.price <= cap + 1e-12),
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


def simulate_sell(book: Book, shares: float, signal_price: float,
                  cfg: PaperConfig, *, strict: bool = False
                  ) -> tuple[Execution | None, str | None]:
    """Hit bids, walking levels down to the allowed floor.

    ``strict`` is used for target exits: accept any available bid, partial
    fills included, so a target liquidation cannot leave us holding an
    orphaned position.  The caller retries any unfilled remainder.
    """
    if strict:
        eligible = sorted(book.bids, key=lambda lv: -lv.price)
        filled, proceeds = _walk(eligible, shares)
        if filled <= 0:
            return None, "no_liquidity"
        return Execution(filled, proceeds / filled - cfg.extra_slippage), None

    floor = sell_floor(signal_price, cfg, book.tick_size)
    eligible = sorted((lv for lv in book.bids if lv.price >= floor - 1e-12),
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


def best_bid_ask(book: Book) -> tuple[float | None, float | None]:
    bid = max((lv.price for lv in book.bids), default=None)
    ask = min((lv.price for lv in book.asks), default=None)
    return bid, ask
