"""Copy-trade engine: poll target wallets, size, simulate fills, settle."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import broker, rules
from .api import ClobApi, DataApi, GammaApi
from .config import AppConfig, TargetConfig, load_config
from .store import Store


class Engine:
    def __init__(self, cfg: AppConfig, store: Store, data_api: DataApi,
                 clob_api: ClobApi, gamma_api: GammaApi,
                 logger: logging.Logger | None = None):
        self.cfg = cfg
        self.store = store
        self.data = data_api
        self.clob = clob_api
        self.gamma = gamma_api
        self.log = logger or logging.getLogger("polycopy.engine")
        self._last_settle = 0
        self._last_mark = 0
        self._market_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._token_cache: dict[str, tuple[float, list[str]]] = {}
        self._cycles = 0
        self._cfg_mtime: float | None = None

    def _maybe_reload_config(self) -> None:
        """Pick up edits made in the web dashboard without a restart."""
        path = self.cfg.path
        if path is None:
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        if self._cfg_mtime is None:
            self._cfg_mtime = mtime
            return
        if mtime <= self._cfg_mtime + 1e-9:
            return
        try:
            new_cfg = load_config(path)
        except Exception:
            self.log.exception("config reload failed, keeping current config")
            self._cfg_mtime = mtime
            return
        self.cfg = new_cfg
        self._cfg_mtime = mtime
        self.log.info("config reloaded from %s", path)

    # ------------------------------------------------------------------ cycle
    def cycle(self) -> None:
        self._maybe_reload_config()
        now = int(time.time())
        for name, target in self.cfg.targets.items():
            if not target.enabled:
                continue
            try:
                self.process_target(name, target)
            except Exception:
                self.log.exception("target %s failed", name)

        if now - self._last_settle >= self.cfg.paper.settle_every_seconds:
            try:
                self.settle()
                self._last_settle = now
            except Exception:
                self.log.exception("settlement failed")

        if now - self._last_mark >= self.cfg.paper.mark_every_seconds:
            try:
                self.mark()
                self._last_mark = now
            except Exception:
                self.log.exception("marking failed")

        self.store.set_kv("bot_heartbeat", str(int(time.time())))
        self.store.commit()
        self._cycles += 1
        if self._cycles % 30 == 1:
            acc = self.store.account()
            self.log.info(
                "heartbeat open=%d cash=$%.2f realized=$%.2f",
                self.store.count_open_positions(), acc["cash"], acc["realized_pnl"],
            )

    def run(self, cycles: int | None = None) -> None:
        while True:
            started = time.time()
            self.cycle()
            if cycles is not None:
                cycles -= 1
                if cycles <= 0:
                    return
            delay = self.cfg.paper.poll_seconds - (time.time() - started)
            if delay > 0:
                time.sleep(delay)

    # -------------------------------------------------------------- target poll
    def process_target(self, name: str, target: TargetConfig) -> None:
        address = target.address.lower()
        cursor_key = f"cursor:{address}"
        cursor = int(self.store.get_kv(cursor_key, "0") or 0)
        if cursor <= 0:
            cursor = int(time.time()) - max(0, target.initial_lookback_seconds)
            self.store.set_kv(cursor_key, str(cursor))
        start = max(0, cursor - self.cfg.paper.overlap_seconds)

        rows = self.data.activity(
            address, start_ts=start,
            limit=self.cfg.paper.page_limit,
            max_pages=self.cfg.paper.max_pages,
        )
        new_rows = []
        for row in rows:
            ts = int(row.get("timestamp") or 0)
            if ts < start:
                continue
            key = rules.dedupe_key(row)
            if self.store.insert_target_fill(key, address, row):
                new_rows.append(row)

        for row in sorted(new_rows, key=lambda r: int(r.get("timestamp") or 0)):
            try:
                self._handle_row(name, target, row)
            except Exception:
                self.log.exception("row handling failed target=%s row=%s",
                                   name, json.dumps(row, ensure_ascii=False)[:400])

        if rows:
            newest = max(int(r.get("timestamp") or 0) for r in rows)
            if newest > cursor:
                self.store.set_kv(cursor_key, str(newest))

    def _handle_row(self, name: str, target: TargetConfig,
                    row: dict[str, Any]) -> None:
        kind = (row.get("type") or "").upper()
        ts = int(row.get("timestamp") or 0)
        cond = str(row.get("condition_id") or "")
        token = str(row.get("token_id") or "")
        size = rules.to_float(row.get("size"))

        if kind == "TRADE":
            side = (row.get("side") or "").upper()
            if side == "SELL":
                before = self.store.target_shares(target.address, token)
                frac = min(1.0, size / before) if before > 1e-9 else 0.0
                self.store.add_target_shares(target.address, cond, token, -size)
                if not target.copy_sells:
                    self._skip(target, row, "sells_disabled")
                elif frac <= 0:
                    self._skip(target, row, "target_no_position")
                else:
                    self._copy_sell(name, target, row, frac)
            else:
                self.store.add_target_shares(target.address, cond, token, size)
                self._copy_buy(name, target, row)
        elif kind == "REDEEM":
            self.store.set_target_shares(target.address, token, 0.0)
        elif kind in ("SPLIT", "MERGE"):
            tokens = self._condition_tokens(cond)
            if not tokens:
                tokens = self.store.target_tokens_for_condition(target.address, cond)
            delta = size if kind == "SPLIT" else -size
            for tok in tokens:
                self.store.add_target_shares(target.address, cond, tok, delta)

    # --------------------------------------------------------------- copy buys
    def _copy_buy(self, name: str, target: TargetConfig,
                  row: dict[str, Any]) -> None:
        cond = str(row.get("condition_id") or "")
        token = str(row.get("token_id") or "")
        signal_price = rules.to_float(row.get("price"))
        usd = rules.target_usd(row)
        now = int(time.time())

        existing = self.store.position(target.address, cond, token)
        has_position = bool(existing and existing.get("status") == "OPEN")
        market_usd = self.store.market_exposure(cond)
        reason = rules.buy_skip_reason(
            target, row, now=now,
            stale_seconds=self.cfg.paper.stale_signal_seconds,
            open_positions=self.store.count_open_positions(),
            has_position=has_position, market_usd=market_usd,
        )
        if reason:
            self._skip(target, row, reason)
            return

        account = self.store.account()
        cash = float(account["cash"])
        available = max(0.0, cash * (1.0 - self.cfg.paper.cash_reserve_pct))
        remaining_market = max(0.0, target.max_market_usd - market_usd)
        budget = min(rules.desired_notional(target, usd), remaining_market, available)

        if budget < target.min_notional_usd:
            need = target.min_notional_usd
            if available < need:
                self._skip(target, row, "insufficient_cash")
                return
            if remaining_market < need:
                self._skip(target, row, "max_market_exposure")
                return
            budget = need

        price_ref = max(signal_price, 1e-6)
        shares = budget / price_ref
        if shares < self.cfg.paper.min_shares:
            need = self.cfg.paper.min_shares * price_ref
            if (need > target.max_notional_usd or need > remaining_market
                    or need > available):
                self._skip(target, row, "below_min_size")
                return
            shares = self.cfg.paper.min_shares
            budget = need

        payload = self.clob.book(token)
        if not payload:
            self._skip(target, row, "book_unavailable")
            return
        book = broker.parse_book(payload)
        ex, reason = broker.simulate_buy(book, shares, signal_price, self.cfg.paper)
        if ex is None:
            self._skip(target, row, reason or "no_fill")
            return

        fee_rate = self._fee_rate(target)
        notional = ex.shares * ex.price
        fee = broker.taker_fee(ex.shares, ex.price, fee_rate)
        if (notional > remaining_market + 1e-9
                or notional + fee > available + 1e-9):
            scale = 1.0
            if notional > 0:
                scale = min(scale, remaining_market / notional)
            if notional + fee > 0:
                scale = min(scale, available / (notional + fee))
            scaled = ex.shares * max(0.0, scale)
            if scaled * ex.price < target.min_notional_usd:
                reason = ("insufficient_cash" if available < remaining_market
                          else "max_market_exposure")
                self._skip(target, row, reason)
                return
            ex2, reason = broker.simulate_buy(book, scaled, signal_price, self.cfg.paper)
            if ex2 is None:
                self._skip(target, row, reason or "no_fill")
                return
            ex = ex2
            notional = ex.shares * ex.price
            fee = broker.taker_fee(ex.shares, ex.price, fee_rate)

        self.store.apply_buy(
            ts=now, target=target.address, signal_id=None, condition_id=cond,
            token_id=token, outcome=str(row.get("outcome") or ""),
            title=str(row.get("title") or ""),
            event_slug=str(row.get("event_slug") or ""),
            shares=ex.shares, price=ex.price, fee=fee,
        )
        self._record_signal(
            target, row, copied=True, reason=None, our_shares=ex.shares,
            our_price=ex.price, our_notional=notional, our_fee=fee,
        )
        self.log.info(
            "COPY BUY %-8s px=%.3f sh=%.2f $=%.2f fee=%.4f latency=%.1fs %s",
            name, ex.price, ex.shares, notional, fee,
            time.time() - int(row.get("timestamp") or 0),
            str(row.get("title") or "")[:60],
        )

    # -------------------------------------------------------------- copy sells
    def _copy_sell(self, name: str, target: TargetConfig,
                   row: dict[str, Any], frac: float) -> None:
        cond = str(row.get("condition_id") or "")
        token = str(row.get("token_id") or "")
        signal_price = rules.to_float(row.get("price"))
        pos = self.store.position(target.address, cond, token)
        if not pos or pos.get("status") != "OPEN" or float(pos["shares"]) <= 0:
            self._skip(target, row, "no_position")
            return

        our_shares = float(pos["shares"]) * frac
        if our_shares < self.cfg.paper.min_shares:
            self._skip(target, row, "dust")
            return

        payload = self.clob.book(token)
        if not payload:
            self._skip(target, row, "book_unavailable")
            return
        book = broker.parse_book(payload)
        ex, reason = broker.simulate_sell(book, our_shares, signal_price, self.cfg.paper)
        if ex is None:
            self._skip(target, row, reason or "no_fill")
            return

        fee = broker.taker_fee(ex.shares, ex.price, self._fee_rate(target))
        result = self.store.apply_sell(
            ts=int(time.time()), target=target.address, signal_id=None,
            condition_id=cond, token_id=token,
            outcome=str(row.get("outcome") or ""), shares=ex.shares,
            price=ex.price, fee=fee,
        )
        if result is None:
            self._skip(target, row, "no_position")
            return
        self._record_signal(
            target, row, copied=True, reason=None, our_shares=ex.shares,
            our_price=ex.price, our_notional=result["notional"], our_fee=fee,
        )
        self.log.info(
            "COPY SELL %-8s px=%.3f sh=%.2f $=%.2f fee=%.4f frac=%.2f %s",
            name, ex.price, ex.shares, result["notional"], fee, frac,
            str(row.get("title") or "")[:60],
        )

    # ------------------------------------------------------------------ helpers
    def _skip(self, target: TargetConfig, row: dict[str, Any], reason: str) -> None:
        self._record_signal(target, row, copied=False, reason=reason)
        self.log.debug("skip %s %s px=%s usd=%.4f",
                       reason, target.address, row.get("price"),
                       rules.target_usd(row))

    def _record_signal(self, target: TargetConfig, row: dict[str, Any], *,
                       copied: bool, reason: str | None,
                       our_shares: float | None = None,
                       our_price: float | None = None,
                       our_notional: float | None = None,
                       our_fee: float | None = None) -> None:
        ts = int(row.get("timestamp") or 0)
        self.store.record_signal(
            target=target.address,
            dedupe_key=rules.dedupe_key(row),
            ts=ts,
            latency_seconds=max(0.0, time.time() - ts) if ts else None,
            side=(row.get("side") or "").upper(),
            condition_id=str(row.get("condition_id") or ""),
            token_id=str(row.get("token_id") or ""),
            outcome=str(row.get("outcome") or ""),
            price=rules.to_float(row.get("price")),
            size=rules.to_float(row.get("size")),
            usd=rules.target_usd(row),
            title=str(row.get("title") or ""),
            event_slug=str(row.get("event_slug") or ""),
            copied=copied, reason=reason, our_shares=our_shares,
            our_price=our_price, our_notional=our_notional, our_fee=our_fee,
        )

    def _fee_rate(self, target: TargetConfig) -> float:
        if target.fee_rate is not None:
            return float(target.fee_rate)
        return float(self.cfg.paper.fee_rate)

    def _condition_tokens(self, condition_id: str) -> list[str]:
        now = time.time()
        entry = self._token_cache.get(condition_id)
        if entry and now - entry[0] < 3600:
            return entry[1]
        market = self._market(condition_id)
        tokens: list[str] = []
        if market:
            try:
                raw = market.get("clobTokenIds") or "[]"
                tokens = [str(x) for x in json.loads(raw)]
            except (TypeError, ValueError, json.JSONDecodeError):
                tokens = []
        self._token_cache[condition_id] = (now, tokens)
        return tokens

    def _market(self, condition_id: str) -> dict[str, Any] | None:
        now = time.time()
        entry = self._market_cache.get(condition_id)
        if entry:
            fetched, market = entry
            if market.get("closed") or now - fetched < 300:
                return market
        market = self.gamma.market_by_condition(condition_id)
        if market:
            self._market_cache[condition_id] = (now, market)
        return market

    # ---------------------------------------------------------------- settlement
    def settle(self) -> int:
        now = int(time.time())
        settled = 0
        for cond in self.store.open_conditions():
            market = self._market(cond)
            if not market or not market.get("closed"):
                continue
            outcomes = _json_list(market.get("outcomes"))
            prices = _json_list(market.get("outcomePrices"))
            if len(outcomes) != len(prices) or not prices:
                continue
            try:
                values = [float(x) for x in prices]
            except (TypeError, ValueError):
                continue
            if max(values) < 0.99 or min(values) > 0.01:
                continue
            winner = outcomes[values.index(max(values))]
            for pos in self.store.positions_for_condition(cond):
                payout = 1.0 if _norm(pos.get("outcome")) == _norm(winner) else 0.0
                realized = self.store.settle_position(pos, payout, now)
                settled += 1
                self.log.info(
                    "SETTLE %s winner=%s our=%s payout=%.2f realized=$%.2f %s",
                    cond[:10], winner, pos.get("outcome"), payout, realized,
                    str(pos.get("title") or "")[:50],
                )
        self.store.commit()
        return settled

    # --------------------------------------------------------------------- marks
    def mark(self) -> int:
        now = int(time.time())
        positions = self.store.open_positions(limit=self.cfg.paper.mark_limit)
        marked = 0
        for pos in positions:
            payload = self.clob.book(pos["token_id"])
            if not payload:
                continue
            bid, ask = broker.best_bid_ask(broker.parse_book(payload))
            if bid is None and ask is None:
                continue
            if bid is None:
                mid = ask
            elif ask is None:
                mid = bid
            else:
                mid = (bid + ask) / 2.0
            self.store.set_mark(pos["token_id"], bid, ask, mid, now)
            marked += 1
            time.sleep(0.05)
        self.store.commit()
        return marked


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except json.JSONDecodeError:
            pass
    return []


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()
