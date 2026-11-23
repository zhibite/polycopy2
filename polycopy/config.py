"""Configuration loading (TOML, stdlib only)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass
class AccountConfig:
    starting_cash: float = 10_000.0


@dataclass
class PaperConfig:
    poll_seconds: int = 5
    overlap_seconds: int = 900
    stale_signal_seconds: int = 300
    fee_rate: float = 0.01          # Polymarket: fee = shares * price * (1-price) * fee_rate
    extra_slippage: float = 0.0     # added to buy VWAP / subtracted from sell VWAP
    min_fill_ratio: float = 0.5     # skip when the book can only fill less than this share
    max_buy_drift: float = 0.03     # max price above the target's fill we will pay
    max_sell_drift: float = 0.10    # max price below the target's fill we will accept
    settle_every_seconds: int = 300
    mark_every_seconds: int = 300
    mark_limit: int = 100
    max_pages: int = 3
    page_limit: int = 500
    cash_reserve_pct: float = 0.05
    min_shares: float = 1.0         # paper-only guard; live CLOB minimum is usually 5 shares
    request_timeout: int = 20
    request_retries: int = 4
    db_path: str = "data/paper.db"
    log_path: str = "data/paper.log"


@dataclass
class TargetConfig:
    address: str
    enabled: bool = True
    sizing_mode: str = "scale"      # "scale" | "fixed"
    scale_factor: float = 0.02      # our notional = target notional * scale_factor
    fixed_notional_usd: float = 10.0
    min_notional_usd: float = 2.0
    max_notional_usd: float = 100.0
    buy_price_min: float = 0.30
    buy_price_max: float = 0.70
    min_target_usd: float = 20.0
    max_target_usd: float = 1e12
    copy_sells: bool = True
    initial_lookback_seconds: int = 300
    max_market_usd: float = 300.0
    max_open_positions: int = 100
    fee_rate: float | None = None   # per-target override (e.g. 0.05 for BTC 5m/15m)
    exclude_event_substrings: list[str] = field(default_factory=list)
    exclude_slug_substrings: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    account: AccountConfig
    paper: PaperConfig
    targets: dict[str, TargetConfig]
    path: Path | None = None


def _filter_fields(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    allowed = {f.name for f in fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown config keys for {cls.__name__}: {sorted(unknown)}")
    return dict(data)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    account = AccountConfig(**_filter_fields(AccountConfig, raw.get("account", {})))
    paper = PaperConfig(**_filter_fields(PaperConfig, raw.get("paper", {})))

    targets: dict[str, TargetConfig] = {}
    for name, tdata in (raw.get("targets") or {}).items():
        if not isinstance(tdata, dict) or not tdata.get("address"):
            raise ValueError(f"target {name!r} is missing an address")
        targets[name] = TargetConfig(**_filter_fields(TargetConfig, tdata))
    if not targets:
        raise ValueError("config must define at least one [targets.<name>]")

    return AppConfig(account=account, paper=paper, targets=targets, path=path)
