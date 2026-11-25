"""FastAPI dashboard for the polycopy paper-trading account.

Server-rendered Jinja templates, Chinese UI, cookie sessions. The app only
reads the SQLite file written by the bot (`PRAGMA query_only`) and can edit
the TOML engine config (the engine hot-reloads it).
"""
from __future__ import annotations

import hashlib
import secrets
import time
import urllib.parse
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import AppConfig, RunConfig, load_config
from . import auth, config_io, queries

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

app = FastAPI(
    title="PolyCopy 纸盘", docs_url=None, redoc_url=None, openapi_url=None
)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------

def _num(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    return f"{number:,.{digits}f}"


def _money(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.{digits}f}"


def _signed_money(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    return f"{'+' if number >= 0 else '-'}${abs(number):,.{digits}f}"


def _pct(value: Any, digits: int = 2) -> str:
    try:
        return f"{100 * float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return "--"


def _price(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "--"


def _dt_bj(ts: Any) -> str:
    if not ts:
        return "--"
    try:
        return datetime.fromtimestamp(int(ts), queries.BJ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "--"


def _date_bj(ts: Any) -> str:
    if not ts:
        return "--"
    try:
        return datetime.fromtimestamp(int(ts), queries.BJ).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return "--"


def _short_addr(value: Any) -> str:
    text = str(value or "")
    return text[:6] + "…" + text[-4:] if len(text) > 12 else text


def _short_title(value: Any, length: int = 58) -> str:
    text = str(value or "")
    return text if len(text) <= length else text[: length - 1] + "…"


def _market_url(event_slug: Any) -> str | None:
    slug = str(event_slug or "").strip()
    if not slug:
        return None
    return "https://polymarket.com/event/" + urllib.parse.quote(
        slug, safe="-_.~")


REASON_CN = {
    "price_out_of_range": "价格不在区间",
    "target_too_small": "目标成交额过小",
    "target_too_large": "目标成交额过大",
    "stale": "信号过期",
    "event_excluded": "事件被排除",
    "slug_excluded": "市场被排除",
    "max_open_positions": "持仓数上限",
    "max_market_exposure": "单市场敞口上限",
    "insufficient_cash": "现金不足",
    "book_unavailable": "盘口不可用",
    "drift": "滑点超限",
    "no_liquidity": "无流动性",
    "insufficient_depth": "深度不足",
    "below_min_size": "低于最小下单量",
    "manual_credit_absorbed": "手动卖出已覆盖本次目标卖出",
    "sells_disabled": "未开启跟卖",
    "target_no_position": "目标无仓位",
    "no_position": "我方无仓位",
    "dust": "仓位过小",
    "no_fill": "未成交",
    "sells_disabled": "未开启跟卖",
}


def _reason_cn(value: Any) -> str:
    if value is None or value == "":
        return "已跟单"
    return REASON_CN.get(str(value), str(value))


templates.env.filters.update({
    "num": _num,
    "money": _money,
    "signed_money": _signed_money,
    "pct": _pct,
    "price": _price,
    "dt_bj": _dt_bj,
    "date_bj": _date_bj,
    "short_addr": _short_addr,
    "short_title": _short_title,
    "market_url": _market_url,
    "reason_cn": _reason_cn,
})


def _asset_version() -> str:
    digest = hashlib.md5()
    for name in ("style.css",):
        path = STATIC_DIR / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10] or "1"


ASSET_VERSION = _asset_version()


@app.middleware("http")
async def _headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_cfg() -> AppConfig | None:
    try:
        return load_config(queries.CONFIG_PATH)
    except Exception:
        return None


def _bot_online() -> tuple[bool, int | None, str | None, str | None]:
    conn = queries.connect()
    try:
        heartbeat = queries.kv_int(conn, "bot_heartbeat")
        mode = queries.kv_str(conn, "execution_mode")
        dry_run = queries.kv_str(conn, "execution_dry_run")
    finally:
        queries.close(conn)
    online = bool(heartbeat and time.time() - heartbeat <= 90)
    return online, heartbeat, mode, dry_run


def _render(request: Request, name: str, page: str, **ctx: Any) -> HTMLResponse:
    session = auth.current_session(request)
    online, heartbeat, db_mode, db_dry = _bot_online()
    cfg = _load_cfg()
    configured_mode = cfg.run.mode if cfg else "unknown"
    actual_mode = db_mode or configured_mode
    dry_run = (db_dry == "1") if db_dry is not None else (
        cfg.live.dry_run if cfg else True)
    context = {
        "request": request,
        "page": page,
        "username": session.user if session else "",
        "now_bj": _dt_bj(int(time.time())),
        "bot_online": online,
        "bot_heartbeat": heartbeat,
        "asset_version": ASSET_VERSION,
        "run_mode": actual_mode,
        "live_dry_run": dry_run,
        "config_mode": configured_mode,
        "mode_mismatch": bool(db_mode and db_mode != configured_mode),
        "min_sell_shares": (
            cfg.live.min_order_shares if actual_mode == "live"
            else cfg.paper.min_shares) if cfg else 1.0,
    }
    context.update(ctx)
    return templates.TemplateResponse(request=request, name=name, context=context)


def _login_redirect(request: Request) -> RedirectResponse | None:
    if auth.current_session(request) is not None:
        return None
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse(
        "/login?next=" + urllib.parse.quote(target), status_code=302
    )


def _parse_date(value: str, end_of_day: bool = False) -> int | None:
    if not value:
        return None
    try:
        moment = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=queries.BJ)
    except ValueError:
        return None
    if end_of_day:
        moment += timedelta(days=1)
    return int(moment.timestamp())


def _require_api(request: Request) -> None:
    if auth.current_session(request) is None:
        raise HTTPException(status_code=401, detail="需要登录")


def _render_login(request: Request, next_url: str, error: str | None,
                  status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "next": auth.safe_next(next_url),
            "error": error,
            "now_bj": _dt_bj(int(time.time())),
            "asset_version": ASSET_VERSION,
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if auth.current_session(request) is not None:
        return RedirectResponse(auth.safe_next(next), status_code=302)
    return _render_login(request, next, None)


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    ip = auth.client_ip(request)
    if auth.login_blocked(ip):
        return _render_login(
            request, next, "登录失败次数过多，请 5 分钟后再试。", status_code=429
        )
    if not auth.verify_credentials(username, password):
        auth.record_login_failure(ip)
        return _render_login(request, next, "用户名或密码错误。", status_code=401)
    auth.reset_login_failures(ip)
    token = auth.create_session(username)
    response = RedirectResponse(auth.safe_next(next), status_code=302)
    auth.set_session_cookie(response, token, request)
    return response


@app.post("/logout")
def logout(request: Request):
    auth.delete_session(request.cookies.get(auth.SESSION_COOKIE))
    response = RedirectResponse("/login", status_code=302)
    auth.clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    redirect = _login_redirect(request)
    if redirect:
        return redirect
    conn = queries.connect()
    try:
        summary = queries.summary(conn)
        positions = queries.open_positions(conn, limit=25)
        fills = queries.recent_fills(conn, limit=15)
        skips = queries.signal_summary(conn)
        daily = queries.daily_pnl(conn, days=14)
        targets = queries.target_summary(conn)
    finally:
        queries.close(conn)
    return _render(
        request, "dashboard.html", "dashboard", summary=summary,
        positions=positions, fills=fills, skips=skips[:8], daily=daily,
        targets=targets, cfg=_load_cfg(),
    )


@app.get("/trades", response_class=HTMLResponse)
def trades(request: Request, side: str = "", fill_type: str = "",
           target: str = "", start: str = "", end: str = "", page: int = 1):
    redirect = _login_redirect(request)
    if redirect:
        return redirect
    side = side.upper() if side.upper() in ("BUY", "SELL") else ""
    conn = queries.connect()
    try:
        data = queries.fills_page(
            conn, side=side, fill_type=fill_type, target=target,
            start_ts=_parse_date(start), end_ts=_parse_date(end, end_of_day=True),
            page=page, per_page=50,
        )
    finally:
        queries.close(conn)
    return _render(
        request, "trades.html", "trades", data=data, cfg=_load_cfg(),
        filters={"side": side, "fill_type": fill_type, "target": target,
                 "start": start, "end": end},
    )


@app.get("/positions", response_class=HTMLResponse)
def positions(request: Request, status: str = "open", page: int = 1,
              sell_queued: str = "", sell_error: str = ""):
    redirect = _login_redirect(request)
    if redirect:
        return redirect
    session = auth.current_session(request)
    conn = queries.connect()
    try:
        data = queries.positions_page(conn, status=status, page=page, per_page=50)
        settlements = queries.recent_settlements(conn, limit=15)
        queued_manual = queries.pending_manual_sells(conn, limit=20)
    finally:
        queries.close(conn)
    return _render(
        request, "positions.html", "positions", data=data,
        settlements=settlements, cfg=_load_cfg(),
        csrf=session.csrf if session else "",
        queued_manual=queued_manual,
        sell_queued=bool(sell_queued), sell_error=sell_error,
    )


@app.post("/positions/sell")
def positions_sell(request: Request, position_id: int = Form(...),
                   pct: float = Form(...), csrf: str = Form("")):
    session = auth.current_session(request)
    if session is None:
        return RedirectResponse("/login?next=/positions", status_code=302)
    if not secrets.compare_digest(str(csrf or ""), session.csrf):
        raise HTTPException(status_code=400, detail="CSRF 校验失败，请刷新页面重试")
    if pct not in (0.25, 0.5, 1.0):
        return RedirectResponse(
            "/positions?sell_error=" + urllib.parse.quote("卖出比例无效"),
            status_code=303,
        )
    request_id, error = queries.queue_manual_sell(position_id, pct)
    if error:
        return RedirectResponse(
            "/positions?sell_error=" + urllib.parse.quote(str(error)),
            status_code=303,
        )
    return RedirectResponse("/positions?sell_queued=1", status_code=303)


@app.get("/signals", response_class=HTMLResponse)
def signals(request: Request, copied: str = "", reason: str = "", page: int = 1):
    redirect = _login_redirect(request)
    if redirect:
        return redirect
    conn = queries.connect()
    try:
        data = queries.signals_page(
            conn, copied=copied, reason=reason, page=page, per_page=20
        )
        summary = queries.signal_summary(conn)
    finally:
        queries.close(conn)
    return _render(
        request, "signals.html", "signals", data=data, summary=summary,
        cfg=_load_cfg(), filters={"copied": copied, "reason": reason},
    )


@app.get("/config", response_class=HTMLResponse)
def config_page(request: Request, saved: str = "", mode_changed: str = "",
                restart_requested: str = "", restart_error: str = ""):
    redirect = _login_redirect(request)
    if redirect:
        return redirect
    session = auth.current_session(request)
    cfg = _load_cfg()
    if cfg is None or not cfg.targets:
        return HTMLResponse(
            f"<h1>无法读取配置文件</h1><p>{queries.CONFIG_PATH}</p>"
            "<p>请检查文件是否存在且为合法 TOML。</p>",
            status_code=500,
        )
    name = next(iter(cfg.targets))
    return _render(
        request, "config.html", "config", cfg=cfg, target_name=name,
        target=cfg.targets[name], csrf=session.csrf if session else "",
        saved=bool(saved), mode_changed=bool(mode_changed), error=None,
        restart_requested=bool(restart_requested), restart_error=restart_error,
        config_path=str(queries.CONFIG_PATH), db_path=str(queries.DB_PATH),
        run=cfg.run, live=cfg.live,
    )


# ---------------------------------------------------------------------------
# Config save
# ---------------------------------------------------------------------------

def _fnum(form: Any, key: str, default: float, errors: list[str],
          label: str, lo: float | None = None, hi: float | None = None) -> float:
    raw = str(form.get(key, "")).strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        errors.append(f"{label}不是有效数字")
        return default
    if lo is not None and value < lo:
        errors.append(f"{label}不能小于 {lo}")
    if hi is not None and value > hi:
        errors.append(f"{label}不能大于 {hi}")
    return value


def _fint(form: Any, key: str, default: int, errors: list[str],
          label: str, lo: int | None = None, hi: int | None = None) -> int:
    raw = str(form.get(key, "")).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        errors.append(f"{label}不是有效整数")
        return default
    if lo is not None and value < lo:
        errors.append(f"{label}不能小于 {lo}")
    if hi is not None and value > hi:
        errors.append(f"{label}不能大于 {hi}")
    return value


def _fbool(form: Any, key: str) -> bool:
    return str(form.get(key) or "").strip().lower() in ("1", "on", "true", "yes")


def _flist(form: Any, key: str) -> list[str]:
    raw = str(form.get(key, ""))
    return [item.strip() for item in raw.split(",") if item.strip()]


@app.post("/config")
async def config_save(request: Request):
    session = auth.current_session(request)
    if session is None:
        return RedirectResponse("/login?next=/config", status_code=302)
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf") or ""), session.csrf):
        raise HTTPException(status_code=400, detail="CSRF 校验失败，请刷新页面重试")
    cfg = _load_cfg()
    if cfg is None or not cfg.targets:
        raise HTTPException(status_code=500, detail="无法读取当前配置")
    name = next(iter(cfg.targets))
    target = cfg.targets[name]
    errors: list[str] = []

    paper = replace(
        cfg.paper,
        poll_seconds=_fint(form, "paper.poll_seconds", cfg.paper.poll_seconds, errors, "轮询间隔", 2, 3600),
        overlap_seconds=_fint(form, "paper.overlap_seconds", cfg.paper.overlap_seconds, errors, "回溯重叠", 0, 86400),
        stale_signal_seconds=_fint(form, "paper.stale_signal_seconds", cfg.paper.stale_signal_seconds, errors, "信号有效期", 0, 604800),
        fee_rate=_fnum(form, "paper.fee_rate", cfg.paper.fee_rate, errors, "手续费率", 0, 0.5),
        max_buy_drift=_fnum(form, "paper.max_buy_drift", cfg.paper.max_buy_drift, errors, "买入滑点上限", 0, 0.5),
        max_sell_drift=_fnum(form, "paper.max_sell_drift", cfg.paper.max_sell_drift, errors, "卖出滑点上限", 0, 0.5),
        max_buy_drift_pct=_fnum(form, "paper.max_buy_drift_pct", cfg.paper.max_buy_drift_pct, errors, "买入相对滑点", 0, 1),
        max_sell_drift_pct=_fnum(form, "paper.max_sell_drift_pct", cfg.paper.max_sell_drift_pct, errors, "卖出相对滑点", 0, 1),
        min_fill_ratio=_fnum(form, "paper.min_fill_ratio", cfg.paper.min_fill_ratio, errors, "最低成交比例", 0, 1),
        cash_reserve_pct=_fnum(form, "paper.cash_reserve_pct", cfg.paper.cash_reserve_pct, errors, "现金保留比例", 0, 0.9),
        settle_every_seconds=_fint(form, "paper.settle_every_seconds", cfg.paper.settle_every_seconds, errors, "结算检查间隔", 30, 86400),
        mark_every_seconds=_fint(form, "paper.mark_every_seconds", cfg.paper.mark_every_seconds, errors, "标记价格间隔", 30, 86400),
        max_pages=_fint(form, "paper.max_pages", cfg.paper.max_pages, errors, "最大分页数", 1, 50),
        page_limit=_fint(form, "paper.page_limit", cfg.paper.page_limit, errors, "每页条数", 50, 1000),
    )

    mode = str(form.get("run.mode") or cfg.run.mode).strip().lower()
    if mode not in ("paper", "live"):
        errors.append("运行模式必须是 paper 或 live")
        mode = cfg.run.mode
    run_cfg = RunConfig(mode=mode)
    live = replace(
        cfg.live,
        dry_run=_fbool(form, "live.dry_run"),
        private_key_env=str(form.get("live.private_key_env") or "").strip(),
        funder=str(form.get("live.funder") or "").strip(),
        signature_type=_fint(form, "live.signature_type", cfg.live.signature_type,
                             errors, "实盘签名类型", 0, 3),
        max_order_usd=_fnum(form, "live.max_order_usd", cfg.live.max_order_usd,
                            errors, "实盘单笔上限", 0.01, 1e9),
        max_total_exposure_usd=_fnum(
            form, "live.max_total_exposure_usd",
            cfg.live.max_total_exposure_usd, errors, "实盘总敞口上限", 0, 1e9),
        max_daily_loss_usd=_fnum(
            form, "live.max_daily_loss_usd", cfg.live.max_daily_loss_usd,
            errors, "实盘日亏损上限", 0, 1e9),
        min_order_shares=_fnum(
            form, "live.min_order_shares", cfg.live.min_order_shares,
            errors, "实盘最小份额", 0, 1e6),
        order_timeout_seconds=_fint(
            form, "live.order_timeout_seconds", cfg.live.order_timeout_seconds,
            errors, "实盘订单超时", 1, 3600),
        kill_switch_path=str(form.get("live.kill_switch_path")
                             or cfg.live.kill_switch_path).strip(),
        redeem=_fbool(form, "live.redeem"),
        max_book_depth_pct=_fnum(
            form, "live.max_book_depth_pct", cfg.live.max_book_depth_pct,
            errors, "盘口深度占比", 0, 1),
        db_path=str(form.get("live.db_path") or cfg.live.db_path).strip(),
        log_path=str(form.get("live.log_path") or cfg.live.log_path).strip(),
    )
    try:
        live.validate()
    except ValueError as exc:
        errors.append(str(exc))
    if live.db_path == paper.db_path:
        errors.append("实盘数据库不能与纸盘数据库相同，请设置独立的 live.db_path")
    if mode == "live" and not live.dry_run:
        if not live.funder:
            errors.append("实盘非 dry-run 必须填写 funder 钱包")
        if not live.private_key_env:
            errors.append("实盘非 dry-run 必须填写私钥环境变量名")

    fee_override_raw = str(form.get("target.fee_rate", "")).strip()
    fee_override: float | None = None
    if fee_override_raw:
        try:
            fee_override = float(fee_override_raw)
            if fee_override < 0 or fee_override > 0.5:
                errors.append("目标手续费率必须在 0 到 0.5 之间")
        except ValueError:
            errors.append("目标手续费率不是有效数字")
            fee_override = target.fee_rate

    new_target = replace(
        target,
        enabled=str(form.get("target.enabled") or "") in ("1", "on", "true", "yes"),
        sizing_mode=str(form.get("target.sizing_mode") or target.sizing_mode),
        scale_factor=_fnum(form, "target.scale_factor", target.scale_factor, errors, "跟单比例", 0.00001, 1),
        fixed_notional_usd=_fnum(form, "target.fixed_notional_usd", target.fixed_notional_usd, errors, "固定跟单金额", 0, 1_000_000),
        min_notional_usd=_fnum(form, "target.min_notional_usd", target.min_notional_usd, errors, "单笔最小金额", 0, 1_000_000),
        max_notional_usd=_fnum(form, "target.max_notional_usd", target.max_notional_usd, errors, "单笔最大金额", 0, 1_000_000),
        buy_price_min=_fnum(form, "target.buy_price_min", target.buy_price_min, errors, "最低买入价", 0, 1),
        buy_price_max=_fnum(form, "target.buy_price_max", target.buy_price_max, errors, "最高买入价", 0, 1),
        min_target_usd=_fnum(form, "target.min_target_usd", target.min_target_usd, errors, "目标最小成交额", 0, 1e12),
        max_target_usd=_fnum(form, "target.max_target_usd", target.max_target_usd, errors, "目标最大成交额", 0, 1e12),
        copy_sells=str(form.get("target.copy_sells") or "") in ("1", "on", "true", "yes"),
        initial_lookback_seconds=_fint(form, "target.initial_lookback_seconds", target.initial_lookback_seconds, errors, "首次回溯秒数", 0, 86400),
        max_market_usd=_fnum(form, "target.max_market_usd", target.max_market_usd, errors, "单市场敞口", 0, 1e9),
        max_open_positions=_fint(form, "target.max_open_positions", target.max_open_positions, errors, "最大持仓数", 1, 10000),
        fee_rate=fee_override,
        exclude_event_substrings=_flist(form, "target.exclude_event_substrings"),
        exclude_slug_substrings=_flist(form, "target.exclude_slug_substrings"),
    )
    if new_target.sizing_mode not in ("scale", "fixed"):
        errors.append("仓位模式必须是 scale 或 fixed")
    if new_target.buy_price_min > new_target.buy_price_max:
        errors.append("最低买入价不能高于最高买入价")
    if new_target.min_notional_usd > new_target.max_notional_usd:
        errors.append("单笔最小金额不能高于单笔最大金额")
    if new_target.min_target_usd > new_target.max_target_usd:
        errors.append("目标最小成交额不能高于最大成交额")

    new_cfg = AppConfig(
        account=cfg.account, paper=paper, run=run_cfg, live=live,
        targets={name: new_target}, path=cfg.path,
    )
    if errors:
        return _render(
            request, "config.html", "config", cfg=new_cfg, target_name=name,
            target=new_target, csrf=session.csrf, saved=False,
            mode_changed=False, error="；".join(errors),
            config_path=str(queries.CONFIG_PATH),
            db_path=str(queries.DB_PATH), run=run_cfg, live=live,
        )
    try:
        config_io.save_config(new_cfg, queries.CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001
        return _render(
            request, "config.html", "config", cfg=new_cfg, target_name=name,
            target=new_target, csrf=session.csrf, saved=False,
            mode_changed=False, error=f"保存失败：{exc}",
            config_path=str(queries.CONFIG_PATH),
            db_path=str(queries.DB_PATH), run=run_cfg, live=live,
        )
    needs_restart = (run_cfg != cfg.run or live != cfg.live)
    restart_error = None
    if _fbool(form, "restart"):
        restart_error = queries.request_restart()
    suffix = "&mode_changed=1" if needs_restart else ""
    if _fbool(form, "restart") and not restart_error:
        suffix += "&restart_requested=1"
    if restart_error:
        suffix += "&restart_error=" + urllib.parse.quote(str(restart_error))
    return RedirectResponse(f"/config?saved=1{suffix}", status_code=303)


# ---------------------------------------------------------------------------
# JSON API (auth required)
# ---------------------------------------------------------------------------

@app.get("/api/health")
def api_health():
    online, heartbeat, mode, dry_run = _bot_online()
    return {
        "status": "ok",
        "ts": int(time.time()),
        "bot_online": online,
        "bot_heartbeat": heartbeat,
        "execution_mode": mode,
        "execution_dry_run": dry_run,
        "db_exists": queries.DB_PATH.exists(),
    }


@app.get("/api/status")
def api_status(request: Request):
    _require_api(request)
    conn = queries.connect()
    try:
        return queries.summary(conn)
    finally:
        queries.close(conn)


@app.get("/api/fills")
def api_fills(request: Request, side: str = "", fill_type: str = "",
              target: str = "", start: str = "", end: str = "", page: int = 1):
    _require_api(request)
    conn = queries.connect()
    try:
        return queries.fills_page(
            conn, side=side.upper() if side.upper() in ("BUY", "SELL") else "",
            fill_type=fill_type, target=target, start_ts=_parse_date(start),
            end_ts=_parse_date(end, end_of_day=True), page=page, per_page=100,
        )
    finally:
        queries.close(conn)


@app.get("/api/positions")
def api_positions(request: Request, status: str = "open", page: int = 1):
    _require_api(request)
    conn = queries.connect()
    try:
        return queries.positions_page(conn, status=status, page=page, per_page=100)
    finally:
        queries.close(conn)


@app.get("/api/signals")
def api_signals(request: Request, copied: str = "", reason: str = "", page: int = 1):
    _require_api(request)
    conn = queries.connect()
    try:
        return queries.signals_page(
            conn, copied=copied, reason=reason, page=page, per_page=100
        )
    finally:
        queries.close(conn)


@app.get("/api/config")
def api_config(request: Request):
    _require_api(request)
    cfg = _load_cfg()
    if cfg is None:
        raise HTTPException(status_code=500, detail="配置读取失败")
    return {"path": str(queries.CONFIG_PATH), "config": asdict(cfg)}
