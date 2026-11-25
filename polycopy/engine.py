"""Copy-trade engine: poll target wallets, size, simulate fills, settle."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import broker, rules
from .api import ClobApi, DataApi, GammaApi
from .config import AppConfig, TargetConfig, load_config
from .execution import Executor, PaperExecutor, TradeIntent
from .store import Store


class Engine:
    def __init__(self, cfg: AppConfig, store: Store, data_api: DataApi,
                 clob_api: ClobApi, gamma_api: GammaApi,
                 logger: logging.Logger | None = None,
                 executor: Executor | None = None):
        self.cfg = cfg
        self.store = store
        self.data = data_api
        self.clob = clob_api
        self.gamma = gamma_api
        self.log = logger or logging.getLogger("polycopy.engine")
        self.executor = executor or PaperExecutor(clob_api, cfg.paper,
                                                  logger=self.log)
        self._last_settle = 0
        self._last_mark = 0
        self._last_target_reconcile = 0
        self._last_balance_sync = 0
        self._market_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._token_cache: dict[str, tuple[float, list[str]]] = {}
        self._cycles = 0
        self._cfg_mtime: float | None = None

    def maybe_reload_config(self) -> None:
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
        if (new_cfg.run != self.cfg.run or new_cfg.live != self.cfg.live):
            self.log.warning(
                "run/live settings changed (%s/%s -> %s/%s); restart required,"
                " keeping the current config",
                self.cfg.run.mode, self.cfg.live.dry_run,
                new_cfg.run.mode, new_cfg.live.dry_run,
            )
            self._cfg_mtime = mtime
            return
        self.cfg = new_cfg
        if isinstance(self.executor, PaperExecutor):
            self.executor.cfg = new_cfg.paper
            self.executor.min_shares = float(new_cfg.paper.min_shares)
        self._cfg_mtime = mtime
        self.log.info("config reloaded from %s", path)

    # ------------------------------------------------------------------ cycle
    def cycle(self) -> None:
        self.maybe_reload_config()
        for name, target in self.cfg.targets.items():
            if not target.enabled:
                continue
            try:
                self.process_target(name, target)
            except Exception:
                self.log.exception("target %s failed", name)
        self.maintenance()

    def maintenance(self) -> None:
        """Settlement, marks and heartbeat; safe to call from any source loop."""
        now = int(time.time())
        if now - self._last_target_reconcile >= self.cfg.paper.target_reconcile_seconds:
            try:
                self.reconcile_target_positions()
                self._last_target_reconcile = now
            except Exception:
                self.log.exception("target position reconciliation failed")
        try:
            self.reconcile_orphan_positions()
        except Exception:
            self.log.exception("orphan reconciliation failed")
        try:
            self.process_manual_sells()
        except Exception:
            self.log.exception("manual sell processing failed")
        try:
            self.retry_pending_exits()
        except Exception:
            self.log.exception("pending exit retry failed")

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

        if (self.executor.is_live and not self.cfg.live.dry_run
                and now - self._last_balance_sync >= 60):
            try:
                self._sync_live_balance()
            except Exception:
                self.log.exception("live balance sync failed")
            self._last_balance_sync = now

        self.store.set_kv("execution_mode",
                          "live" if self.executor.is_live else "paper")
        self.store.set_kv(
            "execution_dry_run",
            "1" if self.executor.is_live and self.cfg.live.dry_run else "0")
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
        self.ingest_rows(name, target, rows, start_ts=start, source="data_api")

        if rows:
            newest = max(int(r.get("timestamp") or 0) for r in rows)
            if newest > cursor:
                self.store.set_kv(cursor_key, str(newest))

    def ingest_rows(self, name: str, target: TargetConfig,
                    rows: list[dict[str, Any]], *, source: str = "data_api",
                    start_ts: int | None = None) -> list[dict[str, Any]]:
        """Record and act on activity rows from any signal source.

        Shared by the data-api poller and the on-chain feed so both paths use
        exactly the same de-duplication, rule gates and paper execution.
        """
        address = target.address.lower()
        new_rows: list[dict[str, Any]] = []
        for row in rows:
            ts = int(row.get("timestamp") or 0)
            if start_ts is not None and ts < start_ts:
                continue
            key = rules.dedupe_key(row)
            if self.store.insert_target_fill(key, address, row):
                new_rows.append(row)

        for row in sorted(new_rows, key=lambda r: int(r.get("timestamp") or 0)):
            try:
                self._handle_row(name, target, row, source=source)
            except Exception:
                self.log.exception("row handling failed target=%s row=%s",
                                   name, json.dumps(row, ensure_ascii=False)[:400])
        return new_rows

    def _handle_row(self, name: str, target: TargetConfig,
                    row: dict[str, Any], *,
                    source: str = "data_api") -> None:
        kind = (row.get("type") or "").upper()
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
                    self._skip(target, row, "sells_disabled", source=source)
                elif frac <= 0:
                    self._skip(target, row, "target_no_position", source=source)
                else:
                    self._copy_sell(name, target, row, frac, source=source)
            else:
                self.store.add_target_shares(target.address, cond, token, size)
                self._copy_buy(name, target, row, source=source)
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
                  row: dict[str, Any], *, source: str = "data_api") -> None:
        cond = str(row.get("condition_id") or "")
        token = str(row.get("token_id") or "")
        signal_price = rules.to_float(row.get("price"))
        usd = rules.target_usd(row)
        now = int(time.time())

        existing = self.store.position(target.address, cond, token)
        has_position = bool(existing and existing.get("status") == "OPEN")
        market_usd = self.store.market_exposure(cond)
        open_positions = self.store.count_open_positions()
        reason = rules.buy_skip_reason(
            target, row, now=now,
            stale_seconds=self.cfg.paper.stale_signal_seconds,
            open_positions=open_positions,
            has_position=has_position, market_usd=market_usd,
        )
        if reason:
            self._skip(target, row, reason, source=source)
            return

        account = self.store.account()
        ledger_cash = float(account["cash"])
        cash = float(self.executor.available_cash(ledger_cash))
        available = max(0.0, cash * (1.0 - self.cfg.paper.cash_reserve_pct))
        remaining_market = max(0.0, target.max_market_usd - market_usd)
        budget = min(rules.desired_notional(target, usd), remaining_market, available)

        if budget < target.min_notional_usd:
            need = target.min_notional_usd
            if available < need:
                self._skip(target, row, "insufficient_cash", source=source)
                return
            if remaining_market < need:
                self._skip(target, row, "max_market_exposure", source=source)
                return
            budget = need

        price_ref = max(signal_price, 1e-6)
        shares = budget / price_ref
        min_shares = float(self.executor.min_shares)
        if shares < min_shares:
            need = min_shares * price_ref
            if (need > target.max_notional_usd or need > remaining_market
                    or need > available):
                self._skip(target, row, "below_min_size", source=source)
                return
            shares = min_shares
            budget = need

        fee_rate = self._fee_rate(target)
        limit_price = broker.buy_cap(signal_price, self.cfg.paper, 0.001)
        intent = TradeIntent(
            dedupe_key=rules.dedupe_key(row), target=target.address,
            condition_id=cond, token_id=token,
            outcome=str(row.get("outcome") or ""),
            title=str(row.get("title") or ""),
            event_slug=str(row.get("event_slug") or ""),
            side="BUY", shares=shares, limit_price=limit_price,
            signal_price=signal_price, fee_rate=fee_rate,
            max_spend=available, max_market=remaining_market,
            min_notional=target.min_notional_usd, source=source,
        )
        result = self.executor.buy(intent)
        if not result.fills:
            reason = result.reason or "no_fill"
            if result.status == "dry_run":
                reason = "dry_run"
            elif result.status == "submitted":
                reason = "order_pending"
            self._skip(target, row, reason, source=source)
            return

        total_shares = result.filled_shares
        total_notional = result.notional
        total_fee = result.fee
        order_id = next((f.order_id for f in result.fills if f.order_id), None)
        for fill in result.fills:
            self.store.apply_buy(
                ts=now, target=target.address, signal_id=None,
                condition_id=cond, token_id=token,
                outcome=str(row.get("outcome") or ""),
                title=str(row.get("title") or ""),
                event_slug=str(row.get("event_slug") or ""),
                slug=str(row.get("slug") or ""),
                shares=fill.shares, price=fill.price, fee=fill.fee,
                order_id=fill.order_id,
            )
        avg_price = total_notional / total_shares if total_shares else 0.0
        self._record_signal(
            target, row, copied=True, reason=None, our_shares=total_shares,
            our_price=avg_price, our_notional=total_notional,
            our_fee=total_fee, source=source,
        )
        title = str(row.get("title") or "")[:60]
        self.log.info(
            "COPY BUY %-8s px=%.3f sh=%.2f $=%.2f fee=%.4f open=%d "
            "mkt=$%.2f cash=$%.2f latency=%.1fs mode=%s%s %s",
            name, avg_price, total_shares, total_notional, total_fee,
            open_positions, market_usd, cash,
            time.time() - int(row.get("timestamp") or 0),
            "live" if self.executor.is_live else "paper",
            "" if order_id is None else f" order={order_id}", title,
        )
        if 0.60 <= signal_price <= 0.70:
            self.log.info(
                "THIN-EDGE 60-70c buy target=%.4f our=%.4f slip=%+.2fc "
                "open=%d mkt=$%.2f cash=$%.2f %s",
                signal_price, avg_price,
                (avg_price - signal_price) * 100.0,
                open_positions, market_usd, cash, title,
            )

    # -------------------------------------------------------------- copy sells
    def _copy_sell(self, name: str, target: TargetConfig,
                   row: dict[str, Any], frac: float, *,
                   source: str = "data_api") -> None:
        cond = str(row.get("condition_id") or "")
        token = str(row.get("token_id") or "")
        signal_price = rules.to_float(row.get("price"))
        strict = self.cfg.paper.strict_exits
        pos = self.store.position(target.address, cond, token)
        if not pos or pos.get("status") != "OPEN" or float(pos["shares"]) <= 0:
            self._skip(target, row, "no_position", source=source)
            return

        current = float(pos["shares"])
        basis = float(pos.get("basis_shares") or 0.0) or current
        credit = float(pos.get("manual_credit") or 0.0)
        if frac >= 0.999:
            # Target is exiting: never leave a manual-sell residual behind.
            normal_sold = basis
            credit_used = credit
            our_shares = current
        else:
            normal_sold = basis * frac
            credit_used = min(credit, normal_sold)
            our_shares = min(current, normal_sold - credit_used)
        if our_shares <= 1e-12:
            self.store.consume_manual_credit(
                target.address, cond, token, normal_sold, credit_used,
                ts=int(time.time()))
            self._skip(target, row, "manual_credit_absorbed", source=source)
            return
        if our_shares < float(self.executor.min_shares):
            if self.executor.is_live or not strict:
                # A live FAK order below the venue minimum would be rejected;
                # strict exits cannot force it, so hold to settlement.
                if strict:
                    self.log.warning(
                        "DUST EXIT %-8s sh=%.4f below min=%.2f;"
                        " holding to settle", name, our_shares,
                        self.executor.min_shares)
                self._skip(target, row, "dust", source=source)
                return

        fee_rate = self._fee_rate(target)
        limit_price = (0.001 if strict
                       else broker.sell_floor(signal_price, self.cfg.paper,
                                              0.001))
        intent = TradeIntent(
            dedupe_key=rules.dedupe_key(row), target=target.address,
            condition_id=cond, token_id=token,
            outcome=str(row.get("outcome") or ""),
            title=str(row.get("title") or ""),
            event_slug=str(row.get("event_slug") or ""),
            side="SELL", shares=our_shares, limit_price=limit_price,
            signal_price=signal_price, fee_rate=fee_rate,
            strict=strict, source=source,
        )
        result = self.executor.sell(intent)
        if not result.fills:
            if result.status == "dry_run":
                self._skip(target, row, "dry_run", source=source)
                return
            if result.status == "submitted":
                self._skip(target, row, "order_pending", source=source)
                return
            reason = result.reason or "no_fill"
            if strict:
                self._queue_exit(target, row, our_shares, reason)
                self._skip(target, row, "exit_pending", source=source)
            else:
                self._skip(target, row, reason, source=source)
            return

        total_shares = result.filled_shares
        total_notional = result.notional
        total_fee = result.fee
        open_positions = self.store.count_open_positions()
        market_usd = self.store.market_exposure(cond)
        cash = float(self.store.account()["cash"])
        applied = None
        sold_total = max(our_shares, 1e-12)
        for fill in result.fills:
            ratio = min(1.0, fill.shares / sold_total)
            sell_result = self.store.apply_sell(
                ts=int(time.time()), target=target.address, signal_id=None,
                condition_id=cond, token_id=token,
                outcome=str(row.get("outcome") or ""), shares=fill.shares,
                price=fill.price, fee=fill.fee,
                note="strict_exit" if strict else None,
                order_id=fill.order_id,
                basis_reduction=normal_sold * ratio,
                credit_reduction=credit_used * ratio,
            )
            if sell_result is not None:
                applied = sell_result
        if applied is None:
            self._skip(target, row, "no_position", source=source)
            return
        avg_price = total_notional / total_shares if total_shares else 0.0
        if strict and total_shares < our_shares - 1e-9:
            remaining = our_shares - total_shares
            self._queue_exit(target, row, remaining, "partial_exit")
            self.log.warning(
                "STRICT EXIT partial %-8s filled=%.4f left=%.4f px=%.4f %s",
                name, total_shares, remaining, avg_price,
                str(row.get("title") or "")[:50],
            )
        self._record_signal(
            target, row, copied=True, reason=None, our_shares=total_shares,
            our_price=avg_price, our_notional=total_notional,
            our_fee=total_fee, source=source,
        )
        self.log.info(
            "COPY SELL %-8s px=%.3f sh=%.2f $=%.2f fee=%.4f frac=%.2f "
            "open=%d mkt=$%.2f cash=$%.2f mode=%s %s",
            name, avg_price, total_shares, total_notional, total_fee, frac,
            open_positions, market_usd, cash,
            "live" if self.executor.is_live else "paper",
            str(row.get("title") or "")[:60],
        )

    def _queue_exit(self, target: TargetConfig, row: dict[str, Any],
                    shares: float, reason: str) -> None:
        self.store.queue_pending_exit(
            target=target.address,
            condition_id=str(row.get("condition_id") or ""),
            token_id=str(row.get("token_id") or ""),
            outcome=str(row.get("outcome") or ""),
            shares=shares, reason=reason, ts=int(time.time()),
        )
        self.store.commit()

    # ------------------------------------------------------------ manual sells
    def _sync_live_balance(self) -> None:
        """Keep ledger cash equal to the CLOB collateral balance.

        Positions are tracked separately, so equity remains cash + marks even
        after a deposit or withdrawal.  P&L shown by the dashboard is based on
        realized + unrealized, not equity - starting_cash, so deposits do not
        look like profit.
        """
        balance = getattr(self.executor, "balance", lambda: None)()
        if balance is None:
            return
        self.store.set_cash(float(balance))
        self.store.set_kv("live_collateral_balance",
                          f"{float(balance):.6f}")
        self.log.info("LIVE BALANCE synced $%.2f", float(balance))

    # ------------------------------------------------------------ manual sells
    def process_manual_sells(self) -> int:
        """Execute queued manual sells from the positions page.

        Manual sells are prepayments against future target sells: they reduce
        current shares but stay in ``basis_shares`` as ``manual_credit``.  The
        copy-sell path consumes that credit first, and a target full exit
        always sells whatever remains.
        """
        now = int(time.time())
        filled_requests = 0
        for req in self.store.pending_manual_sells():
            if now - int(req.get("updated_at") or 0) < 10:
                continue
            request_id = int(req["id"])
            target_addr = str(req["target"])
            cond = str(req["condition_id"])
            token = str(req["token_id"])
            pos = self.store.position(target_addr, cond, token)
            if not pos or pos.get("status") != "OPEN" or float(pos["shares"]) <= 1e-9:
                self.store.update_manual_sell(
                    request_id, status="DONE", reason="no_position", ts=now)
                continue
            remaining = min(float(req["remaining_shares"]),
                            float(pos["shares"]))
            if remaining <= 1e-9:
                self.store.update_manual_sell(
                    request_id, status="DONE", remaining_shares=0.0, ts=now)
                continue
            if (self.executor.is_live
                    and remaining < float(self.executor.min_shares)):
                self.store.update_manual_sell(
                    request_id, status="FAILED", reason="below_min_size",
                    ts=now)
                continue

            target_cfg = next(
                (t for t in self.cfg.targets.values()
                 if t.address.lower() == target_addr.lower()), None)
            fee_rate = (self._fee_rate(target_cfg) if target_cfg
                        else self.cfg.paper.fee_rate)
            attempts = int(req.get("attempts") or 0) + 1
            if attempts > 180:
                self.store.update_manual_sell(
                    request_id, status="FAILED", reason="max_attempts",
                    attempts=attempts, ts=now)
                continue
            intent = TradeIntent(
                dedupe_key=f"manual:{request_id}:{attempts}",
                target=target_addr, condition_id=cond, token_id=token,
                outcome=str(pos.get("outcome") or ""),
                title=str(pos.get("title") or ""),
                event_slug=str(pos.get("event_slug") or ""),
                side="SELL", shares=remaining, limit_price=0.001,
                signal_price=0.0, fee_rate=fee_rate, strict=True,
                source="manual_sell",
            )
            result = self.executor.sell(intent)
            if not result.fills:
                if result.status == "dry_run":
                    self.store.update_manual_sell(
                        request_id, status="DRY_RUN", reason="dry_run",
                        attempts=attempts, ts=now)
                    continue
                terminal = result.reason in (
                    "below_min_size", "live_gateway_unavailable",
                    "submit_error", "duplicate_order",
                )
                self.store.update_manual_sell(
                    request_id,
                    status="FAILED" if terminal else None,
                    reason=result.reason or "no_fill",
                    attempts=attempts, ts=now)
                continue

            filled = min(result.filled_shares, remaining)
            applied = False
            for fill in result.fills:
                share = min(float(fill.shares), max(0.0, remaining))
                if share <= 1e-12:
                    continue
                sell_result = self.store.apply_sell(
                    ts=now, target=target_addr, signal_id=None,
                    condition_id=cond, token_id=token,
                    outcome=str(pos.get("outcome") or ""),
                    shares=share, price=fill.price, fee=fill.fee,
                    note="manual_sell", order_id=fill.order_id, manual=True,
                    kind="MANUAL",
                )
                applied = applied or sell_result is not None
            if not applied:
                self.store.update_manual_sell(
                    request_id, reason="no_position", attempts=attempts,
                    ts=now)
                continue
            self.store.reduce_pending_exit(
                target_addr, cond, token, filled)
            new_remaining = max(0.0, float(req["remaining_shares"]) - filled)
            old_filled = float(req.get("filled_shares") or 0.0)
            old_avg = float(req.get("avg_price") or 0.0)
            new_filled = old_filled + filled
            new_avg = ((old_avg * old_filled + result.notional) / new_filled
                       if new_filled else 0.0)
            status = "DONE" if new_remaining <= 1e-9 else "PARTIAL"
            self.store.update_manual_sell(
                request_id, status=status, remaining_shares=new_remaining,
                filled_shares=new_filled, avg_price=new_avg,
                fee=float(req.get("fee") or 0.0) + result.fee,
                attempts=attempts, ts=now)
            filled_requests += 1
            self.log.warning(
                "MANUAL SELL %-8s px=%.4f sh=%.4f $=%.2f fee=%.4f "
                "remaining=%.4f mode=%s",
                target_addr[:10],
                (result.notional / result.filled_shares
                 if result.filled_shares else 0.0),
                filled, result.notional, result.fee, new_remaining,
                "live" if self.executor.is_live else "paper",
            )
        self.store.commit()
        return filled_requests

    # ------------------------------------------------------------ strict exits
    def retry_pending_exits(self) -> int:
        """Strictly sell any queued exit remainder at the best available bid."""
        now = int(time.time())
        filled = 0
        targets = {t.address.lower(): t for t in self.cfg.targets.values()}
        cooldown = 30 if self.executor.is_live else 0
        for pending in self.store.pending_exits():
            if (cooldown
                    and now - int(pending.get("updated_at") or 0) < cooldown):
                continue
            target = targets.get(str(pending["target"]).lower())
            if target is None:
                self.store.delete_pending_exit(pending["id"])
                continue
            cond = str(pending["condition_id"])
            token = str(pending["token_id"])
            pos = self.store.position(target.address, cond, token)
            if not pos or pos.get("status") != "OPEN" or float(pos["shares"]) <= 0:
                self.store.delete_pending_exit(pending["id"])
                continue
            market = self._market(cond)
            if market and market.get("closed"):
                # Settlement closes it; keep the pending row until then.
                continue
            shares = min(float(pending["shares"]), float(pos["shares"]))
            if (self.executor.is_live
                    and shares < float(self.executor.min_shares)):
                self.log.warning(
                    "DUST EXIT drop pending id=%s sh=%.4f below min=%.2f",
                    pending["id"], shares, self.executor.min_shares)
                self.store.delete_pending_exit(pending["id"])
                continue
            attempts = int(pending["attempts"]) + 1
            intent = TradeIntent(
                dedupe_key=f"pending:{pending['id']}:{attempts}",
                target=target.address, condition_id=cond, token_id=token,
                outcome=str(pending.get("outcome") or ""),
                title=str(pos.get("title") or ""),
                event_slug=str(pos.get("event_slug") or ""),
                side="SELL", shares=shares, limit_price=0.001,
                signal_price=0.0, fee_rate=self._fee_rate(target),
                strict=True, source="strict_exit",
            )
            result = self.executor.sell(intent)
            if not result.fills:
                if result.status == "dry_run":
                    continue
                self.store.update_pending_exit(
                    pending["id"], shares=shares, attempts=attempts,
                    reason=result.reason or "no_fill", ts=now,
                )
                continue
            applied = None
            for fill in result.fills:
                applied = self.store.apply_sell(
                    ts=now, target=target.address, signal_id=None,
                    condition_id=cond, token_id=token,
                    outcome=str(pending.get("outcome") or ""),
                    shares=fill.shares, price=fill.price, fee=fill.fee,
                    note="strict_exit", order_id=fill.order_id,
                ) or applied
            if applied is None:
                self.store.delete_pending_exit(pending["id"])
                continue
            remaining = shares - result.filled_shares
            if remaining > 1e-9:
                self.store.update_pending_exit(
                    pending["id"], shares=remaining,
                    attempts=attempts, reason="partial", ts=now,
                )
            else:
                self.store.delete_pending_exit(pending["id"])
            filled += 1
            avg = (result.notional / result.filled_shares
                   if result.filled_shares else 0.0)
            self.log.info(
                "STRICT EXIT %-8s px=%.4f sh=%.3f $=%.2f fee=%.4f "
                "remaining=%.4f mode=%s %s",
                target.address[:10], avg, result.filled_shares,
                result.notional, result.fee, max(0.0, remaining),
                "live" if self.executor.is_live else "paper",
                str(pending.get("reason") or ""),
            )
        self.store.commit()
        return filled

    def reconcile_target_positions(self) -> int:
        """Replace target_positions with the target's actual open positions.

        Uses data-api /positions ``current_size`` only as a ledger source; the
        copy signals still come exclusively from the on-chain feed.  This is
        what keeps sell fractions correct after MERGE/REDEEM/SPLIT events and
        after positions opened before the current process started.
        """
        now = int(time.time())
        total = 0
        for name, target in self.cfg.targets.items():
            if not target.enabled:
                continue
            rows = self.data.open_positions(
                target.address, limit=500, max_pages=10,
            )
            if rows is None:
                self.log.warning(
                    "target ledger reconciliation failed target=%s", name)
                continue
            positions: dict[str, tuple[str, float]] = {}
            for row in rows:
                token = str(row.get("token_id") or "")
                if not token:
                    continue
                size = rules.to_float(row.get("current_size"))
                if size <= 1e-12:
                    size = rules.to_float(row.get("size"))
                if size <= 1e-12:
                    continue
                positions[token] = (
                    str(row.get("condition_id") or ""), size,
                )
            self.store.replace_target_positions(
                target.address,
                [(cond, token, size)
                 for token, (cond, size) in positions.items()],
                now,
            )
            total += len(positions)
            self.log.info(
                "TARGET LEDGER %-8s positions=%d", name, len(positions))
        self.store.commit()
        return total

    def reconcile_orphan_positions(self) -> int:
        """Queue a full strict exit when the target is flat but we still hold."""
        now = int(time.time())
        queued = 0
        for pos in self.store.open_positions(limit=10_000):
            target_addr = str(pos.get("target") or "")
            cond = str(pos.get("condition_id") or "")
            token = str(pos.get("token_id") or "")
            if not target_addr or not token:
                continue
            if self.store.target_shares(target_addr, token) > 1e-9:
                continue
            if self.store.pending_exit(target_addr, cond, token):
                continue
            market = self._market(cond)
            if market and market.get("closed"):
                continue
            self.store.queue_pending_exit(
                target=target_addr, condition_id=cond, token_id=token,
                outcome=str(pos.get("outcome") or ""),
                shares=float(pos["shares"]), reason="target_flat", ts=now,
            )
            queued += 1
            self.log.warning(
                "STRICT EXIT queued orphan %-8s sh=%.4f target_flat %s",
                target_addr[:10], float(pos["shares"]),
                str(pos.get("title") or "")[:50],
            )
        if queued:
            self.store.commit()
        return queued

    # ------------------------------------------------------------------ helpers
    def _skip(self, target: TargetConfig, row: dict[str, Any], reason: str, *,
              source: str = "data_api") -> None:
        self._record_signal(target, row, copied=False, reason=reason,
                            source=source)
        self.log.debug("skip %s %s px=%s usd=%.4f",
                       reason, target.address, row.get("price"),
                       rules.target_usd(row))

    def _record_signal(self, target: TargetConfig, row: dict[str, Any], *,
                       copied: bool, reason: str | None,
                       our_shares: float | None = None,
                       our_price: float | None = None,
                       our_notional: float | None = None,
                       our_fee: float | None = None,
                       source: str = "data_api") -> None:
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
            slug=str(row.get("slug") or ""),
            copied=copied, reason=reason, our_shares=our_shares,
            our_price=our_price, our_notional=our_notional, our_fee=our_fee,
            source=source,
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
        """Resolve closed markets; fetch metadata first, write as one batch."""
        now = int(time.time())
        actions: list[tuple[dict[str, Any], float, str, str]] = []
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
            if self.executor.is_live:
                if not self.cfg.live.redeem:
                    continue
                if not self.executor.redeem(cond):
                    self.log.warning(
                        "SETTLE PENDING redeem failed/unavailable %s;"
                        " position stays claimable", cond[:10])
                    continue
            winner = outcomes[values.index(max(values))]
            for pos in self.store.positions_for_condition(cond):
                payout = 1.0 if _norm(pos.get("outcome")) == _norm(winner) else 0.0
                actions.append((pos, payout, winner, cond))
        settled = 0
        for pos, payout, winner, cond in actions:
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
        """Refresh marks; fetch books first, then write in one short batch.

        Keeping the SQLite write lock out of the HTTP loop matters for the
        on-chain runner, whose signal loop shares the database with the
        background maintenance thread.
        """
        now = int(time.time())
        positions = self.store.open_positions(limit=self.cfg.paper.mark_limit)
        updates: list[tuple[str, float | None, float | None, float | None]] = []
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
            updates.append((pos["token_id"], bid, ask, mid))
            time.sleep(0.05)
        for token_id, bid, ask, mid in updates:
            self.store.set_mark(token_id, bid, ask, mid, now)
        self.store.commit()
        return len(updates)


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
