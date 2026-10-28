import hmac
import json
import logging
import math
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.clock import utcnow
from app.db import database_ok, session_scope
from app.domain_parser import ALLOWED_INTERVALS, parse_csv, parse_domain
from app.models import AppSetting, CheckEvent, Monitor, Proxy, RegistrationSnapshot
from app.notices import notices
from app.presentation import is_overdue, safe_next, section_for
from app.safety import parse_proxy_url, proxy_label
from app.service import (
    delete_monitor,
    move_next_check_if_sooner,
    recent_problems,
    registration_failure_banners,
    running_ids,
    stored_screenshot_path,
    try_enqueue,
    try_enqueue_registration,
)
from app.checks.registration import registration_view
from app.templating import render_template

logger = logging.getLogger(__name__)
router = APIRouter()
PAGE_SIZE = 50


class LoginRequired(Exception):
    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


async def require_user(request: Request) -> None:
    settings = request.app.state.settings
    path = request.url.path
    if path.startswith("/static") or path in {"/login", "/health"}:
        return
    if settings.password and not request.session.get("auth"):
        raise LoginRequired(path)


router.dependencies.append(Depends(require_user))


def _banners() -> list[str]:
    return [*notices.banners(), *registration_failure_banners()]


def render(request: Request, name: str, status_code: int = 200, **extra):
    context = {
        "banners": _banners(),
        "flash": pop_flash(request),
        "csrf": ensure_csrf(request),
        "password_enabled": bool(request.app.state.settings.password),
        "section": section_for(request.url.path),
        "show_nav": True,
        **extra,
    }
    return render_template(request, name, context, status_code=status_code)


def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def csrf_ok(request: Request, form) -> bool:
    expected = request.session.get("csrf") or ""
    received = str(form.get("csrf") or "")
    if not expected or not received:
        return False
    return hmac.compare_digest(expected, received)


def flash(request: Request, message: str) -> None:
    request.session["flash"] = [*request.session.get("flash", []), message]


def pop_flash(request: Request) -> list[str]:
    messages = list(request.session.get("flash", []))
    if messages:
        request.session["flash"] = []
    return messages


def dashboard_context(session: Session) -> dict:
    now = utcnow()
    inflight = running_ids()
    rows = []
    for monitor in session.scalars(select(Monitor).order_by(Monitor.hostname)).all():
        checking = monitor.id in inflight
        rows.append(
            {
                "monitor": monitor,
                "checking": checking,
                "overdue": is_overdue(monitor, now, checking),
            }
        )
    return {"rows": rows, "problems": recent_problems(session)}


@router.get("/health")
def health():
    stalled = notices.scheduler_stalled()
    reachable = database_ok()
    if reachable and not stalled:
        return {"status": "ok"}
    body: dict[str, str] = {"status": "error"}
    if not reachable:
        body["database"] = "unavailable"
    if stalled:
        body["scheduler"] = "stalled"
    return JSONResponse(body, status_code=503)


@router.get("/login")
async def login_form(request: Request):
    settings = request.app.state.settings
    if not settings.password or request.session.get("auth"):
        return RedirectResponse("/", status_code=303)
    return render(
        request,
        "login.html",
        show_nav=False,
        error=None,
        next_path=safe_next(request.query_params.get("next")),
    )


@router.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    settings = request.app.state.settings
    if not csrf_ok(request, form):
        return render(request, "403.html", status_code=403, show_nav=False)
    client_host = request.client.host if request.client else "unknown"
    if notices.login_throttled(client_host):
        return render(
            request,
            "login.html",
            status_code=429,
            show_nav=False,
            error="Too many login attempts. Try again later.",
            next_path=safe_next(str(form.get("next") or "/")),
        )
    password = str(form.get("password") or "")
    expected = settings.password or ""
    if expected and hmac.compare_digest(password.encode(), expected.encode()):
        notices.clear_login_failures(client_host)
        request.session["auth"] = True
        return RedirectResponse(safe_next(str(form.get("next") or "/")), status_code=303)
    notices.record_login_failure(client_host)
    return render(
        request,
        "login.html",
        status_code=401,
        show_nav=False,
        error="Incorrect password.",
        next_path=safe_next(str(form.get("next") or "/")),
    )


@router.post("/logout")
async def logout(request: Request):
    form = await request.form()
    if not csrf_ok(request, form):
        return render(request, "403.html", status_code=403)
    request.session.clear()
    target = "/login" if request.app.state.settings.password else "/"
    return RedirectResponse(target, status_code=303)


@router.get("/notices")
async def notice_fragment(request: Request):
    return render_template(request, "notices.html", {"banners": _banners()})


@router.get("/")
async def monitors(request: Request, session: Session = Depends(session_scope)):
    return render(request, "monitors.html", **dashboard_context(session))


@router.get("/fragment/monitors")
async def monitors_fragment(request: Request, session: Session = Depends(session_scope)):
    return render_template(request, "monitors_live.html", dashboard_context(session))


@router.get("/domains/new")
async def add_form(request: Request):
    return render(request, "add.html", **_add_context())


@router.post("/domains")
async def add_domain(request: Request, session: Session = Depends(session_scope)):
    form = await request.form()
    if not csrf_ok(request, form):
        return render(request, "403.html", status_code=403)
    parsed = parse_domain(str(form.get("domain") or ""))
    minutes, screenshot, crawl, use_proxy, mobile, field_error = _monitor_fields(form)
    error = field_error or parsed.error
    if error:
        return render(
            request,
            "add.html",
            status_code=400,
            **_add_context(domain=str(form.get("domain") or ""), error=error, interval=minutes, screenshot=screenshot, crawl=crawl, use_proxy=use_proxy, mobile=mobile),
        )
    existing = session.scalar(select(Monitor).where(Monitor.hostname == parsed.hostname))
    if existing is not None:
        return render(
            request,
            "add.html",
            status_code=400,
            **_add_context(domain=parsed.hostname, error="This domain is already monitored.", interval=minutes, screenshot=screenshot, crawl=crawl, use_proxy=use_proxy, mobile=mobile),
        )
    now = utcnow()
    monitor = Monitor(
        hostname=parsed.hostname,
        registrable_domain=parsed.registrable_domain,
        interval_minutes=minutes,
        screenshot_enabled=screenshot,
        crawl_enabled=crawl,
        use_proxy=use_proxy,
        mobile_ua=mobile,
        created_at=now,
        updated_at=now,
        next_check_at=now,
        last_partial=False,
    )
    session.add(monitor)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return render(
            request,
            "add.html",
            status_code=400,
            **_add_context(domain=parsed.hostname, error="This domain is already monitored."),
        )
    session.refresh(monitor)
    try_enqueue(monitor.id, "schedule")
    try_enqueue_registration(monitor.id)
    flash(request, f"Added {monitor.hostname}. The first check is queued.")
    return RedirectResponse(f"/domains/{monitor.id}", status_code=303)


@router.post("/domains/csv-preview")
async def csv_preview(request: Request, session: Session = Depends(session_scope)):
    form = await request.form()
    if not csrf_ok(request, form):
        return render(request, "403.html", status_code=403)
    upload = form.get("csv")
    if upload is None or not hasattr(upload, "read"):
        return render(request, "add.html", status_code=400, **_add_context(csv_error="Choose a CSV file."))
    data = await upload.read()
    if len(data) > 1_000_000:
        return render(request, "add.html", status_code=400, **_add_context(csv_error="CSV files must be 1 MB or smaller."))
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return render(request, "add.html", status_code=400, **_add_context(csv_error="CSV must be UTF-8."))
    existing = set(session.scalars(select(Monitor.hostname)).all())
    parsed = parse_csv(text, existing)
    if parsed.file_error:
        return render(request, "add.html", status_code=400, **_add_context(csv_error=parsed.file_error))
    accepted = [row for row in parsed.rows if row.error is None and row.hostname]
    payload = json.dumps(
        [
            {
                "domain": row.hostname,
                "interval_minutes": row.interval_minutes,
                "screenshot": row.screenshot,
                "crawl": row.crawl,
                "use_proxy": row.use_proxy,
                "mobile_ua": row.mobile_ua,
            }
            for row in accepted
        ]
    )
    return render(
        request,
        "add.html",
        **_add_context(csv_rows=parsed.rows, csv_payload=payload, csv_accepted=len(accepted)),
    )


@router.post("/domains/csv-import")
async def csv_import(request: Request, session: Session = Depends(session_scope)):
    form = await request.form()
    if not csrf_ok(request, form):
        return render(request, "403.html", status_code=403)
    try:
        items = json.loads(str(form.get("rows") or "[]"))
    except json.JSONDecodeError:
        items = []
    if not isinstance(items, list):
        items = []
    added = 0
    rejected: list[str] = []
    existing = {host.lower() for host in session.scalars(select(Monitor.hostname)).all()}
    for item in items[:500]:
        if not isinstance(item, dict):
            rejected.append("A row was not a domain.")
            continue
        parsed = parse_domain(str(item.get("domain") or ""))
        if parsed.error or not parsed.hostname:
            rejected.append(parsed.error or "Invalid domain.")
            continue
        if parsed.hostname.lower() in existing:
            rejected.append(f"{parsed.hostname} is already monitored.")
            continue
        try:
            minutes = int(item.get("interval_minutes") or 5)
        except (TypeError, ValueError):
            minutes = 5
        if minutes not in ALLOWED_INTERVALS:
            rejected.append(f"{parsed.hostname} has an invalid interval.")
            continue
        now = utcnow()
        monitor = Monitor(
            hostname=parsed.hostname,
            registrable_domain=parsed.registrable_domain,
            interval_minutes=minutes,
            screenshot_enabled=bool(item.get("screenshot")),
            crawl_enabled=bool(item.get("crawl")),
            use_proxy=bool(item.get("use_proxy")),
            mobile_ua=bool(item.get("mobile_ua")),
            created_at=now,
            updated_at=now,
            next_check_at=now,
            last_partial=False,
        )
        session.add(monitor)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            rejected.append(f"{parsed.hostname} is already monitored.")
            continue
        session.refresh(monitor)
        existing.add(parsed.hostname.lower())
        added += 1
        try_enqueue(monitor.id, "schedule")
        try_enqueue_registration(monitor.id)
    if rejected:
        flash(request, f"Added {added}. Rejected: {'; '.join(rejected[:8])}")
        return RedirectResponse("/domains/new", status_code=303)
    flash(request, f"Added {added} domains.")
    return RedirectResponse("/", status_code=303)


@router.get("/domains/{monitor_id}")
async def detail(request: Request, monitor_id: int, session: Session = Depends(session_scope)):
    monitor = session.get(Monitor, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="This domain is not monitored.")
    total = session.scalar(select(func.count()).select_from(CheckEvent).where(CheckEvent.monitor_id == monitor.id)) or 0
    pages = max(1, math.ceil(total / PAGE_SIZE))
    try:
        page = int(request.query_params.get("page") or 1)
    except ValueError:
        page = 1
    page = min(max(page, 1), pages)
    events = session.scalars(
        select(CheckEvent)
        .where(CheckEvent.monitor_id == monitor.id)
        .options(
            selectinload(CheckEvent.attempts),
            selectinload(CheckEvent.pages),
            selectinload(CheckEvent.screenshots),
        )
        .order_by(CheckEvent.started_at.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    snapshots = session.scalars(
        select(RegistrationSnapshot)
        .where(RegistrationSnapshot.monitor_id == monitor.id)
        .order_by(RegistrationSnapshot.fetched_at.desc())
    ).all()
    proxy_count = session.scalar(select(func.count()).select_from(Proxy).where(Proxy.enabled.is_(True))) or 0
    return render(
        request,
        "detail.html",
        monitor=monitor,
        events=events,
        registration=registration_view(list(snapshots)),
        checking=monitor.id in running_ids(),
        overdue=is_overdue(monitor, utcnow(), monitor.id in running_ids()),
        confirm_delete=request.query_params.get("confirm_delete") == "1",
        page=page,
        pages=pages,
        total=total,
        proxy_count=proxy_count,
    )


@router.post("/domains/{monitor_id}/edit")
async def edit_domain(request: Request, monitor_id: int, session: Session = Depends(session_scope)):
    form = await request.form()
    if not csrf_ok(request, form):
        return render(request, "403.html", status_code=403)
    monitor = session.get(Monitor, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="This domain is not monitored.")
    minutes, screenshot, crawl, use_proxy, mobile, error = _monitor_fields(form)
    if error:
        flash(request, error)
        return RedirectResponse(f"/domains/{monitor.id}", status_code=303)
    monitor.interval_minutes = minutes
    monitor.screenshot_enabled = screenshot
    monitor.crawl_enabled = crawl
    monitor.use_proxy = use_proxy
    monitor.mobile_ua = mobile
    monitor.updated_at = utcnow()
    move_next_check_if_sooner(monitor)
    session.commit()
    flash(request, "Saved.")
    return RedirectResponse(f"/domains/{monitor.id}", status_code=303)


@router.post("/domains/{monitor_id}/check")
async def check_now(request: Request, monitor_id: int, session: Session = Depends(session_scope)):
    form = await request.form()
    if not csrf_ok(request, form):
        return render(request, "403.html", status_code=403)
    monitor = session.get(Monitor, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="This domain is not monitored.")
    if try_enqueue(monitor.id, "manual"):
        flash(request, "Check started.")
    else:
        flash(request, "A check is already running for this domain.")
    return RedirectResponse(f"/domains/{monitor.id}", status_code=303)


@router.post("/domains/{monitor_id}/delete")
async def remove_domain(request: Request, monitor_id: int, session: Session = Depends(session_scope)):
    form = await request.form()
    if not csrf_ok(request, form):
        return render(request, "403.html", status_code=403)
    monitor = session.get(Monitor, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="This domain is not monitored.")
    if form.get("confirm") != "yes":
        return RedirectResponse(f"/domains/{monitor.id}?confirm_delete=1", status_code=303)
    hostname = monitor.hostname
    delete_monitor(session, monitor)
    flash(request, f"Deleted {hostname}.")
    return RedirectResponse("/", status_code=303)


@router.get("/settings")
async def settings_page(request: Request, session: Session = Depends(session_scope)):
    return render(request, "settings.html", **_settings_context(session))


@router.post("/settings/retention")
async def save_retention(request: Request, session: Session = Depends(session_scope)):
    form = await request.form()
    if not csrf_ok(request, form):
        return render(request, "403.html", status_code=403)
    try:
        days = int(form.get("retention_days") or 0)
    except ValueError:
        days = 0
    if days not in {7, 30, 90}:
        return render(request, "settings.html", status_code=400, **_settings_context(session, retention_error="Choose 7, 30, or 90 days."))
    setting = session.get(AppSetting, 1)
    if setting is None:
        setting = AppSetting(id=1, retention_days=days)
        session.add(setting)
    else:
        setting.retention_days = days
    session.commit()
    flash(request, "Retention saved.")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/proxies")
async def add_proxy(request: Request, session: Session = Depends(session_scope)):
    form = await request.form()
    if not csrf_ok(request, form):
        return render(request, "403.html", status_code=403)
    url, error = parse_proxy_url(str(form.get("url") or ""))
    if error or url is None:
        return render(request, "settings.html", status_code=400, **_settings_context(session, proxy_error=error, proxy_value=str(form.get("url") or "")))
    session.add(Proxy(url=url, enabled=True, created_at=utcnow()))
    session.commit()
    flash(request, f"Saved proxy {proxy_label(url)}.")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/proxies/{proxy_id}/toggle")
async def toggle_proxy(request: Request, proxy_id: int, session: Session = Depends(session_scope)):
    form = await request.form()
    if not csrf_ok(request, form):
        return render(request, "403.html", status_code=403)
    proxy = session.get(Proxy, proxy_id)
    if proxy is None:
        raise HTTPException(status_code=404, detail="This proxy was not found.")
    proxy.enabled = not proxy.enabled
    session.commit()
    flash(request, f"{'Enabled' if proxy.enabled else 'Disabled'} {proxy_label(proxy.url)}.")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/proxies/{proxy_id}/delete")
async def delete_proxy(request: Request, proxy_id: int, session: Session = Depends(session_scope)):
    form = await request.form()
    if not csrf_ok(request, form):
        return render(request, "403.html", status_code=403)
    proxy = session.get(Proxy, proxy_id)
    if proxy is None:
        raise HTTPException(status_code=404, detail="This proxy was not found.")
    label = proxy_label(proxy.url)
    session.delete(proxy)
    session.commit()
    flash(request, f"Removed proxy {label}.")
    return RedirectResponse("/settings", status_code=303)


@router.get("/screenshots/{shot_id}")
async def screenshot_file(shot_id: int, session: Session = Depends(session_scope)):
    from app.models import Screenshot

    shot = session.get(Screenshot, shot_id)
    if shot is None or not shot.path:
        raise HTTPException(status_code=404, detail="This screenshot is not available.")
    stored = shot.path
    path = stored_screenshot_path(stored)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="This screenshot is not available.")
    if Path(stored).is_absolute():
        shot.path = path.name
        session.commit()
    return FileResponse(path, media_type="image/jpeg")


def _monitor_fields(form) -> tuple[int, bool, bool, bool, bool, str | None]:
    try:
        minutes = int(form.get("interval_minutes") or 5)
    except (TypeError, ValueError):
        return 5, False, False, False, False, "Choose a valid interval."
    if minutes not in ALLOWED_INTERVALS:
        return 5, False, False, False, False, "Choose a valid interval."
    return (
        minutes,
        _checked(form, "screenshot"),
        _checked(form, "crawl"),
        _checked(form, "use_proxy"),
        _checked(form, "mobile_ua"),
        None,
    )


def _checked(form, name: str) -> bool:
    return str(form.get(name) or "").lower() in {"yes", "on", "true", "1"}


def _add_context(**overrides) -> dict:
    context = {
        "error": None,
        "domain": "",
        "interval": 5,
        "screenshot": False,
        "crawl": False,
        "use_proxy": False,
        "mobile": False,
        "csv_error": None,
        "csv_rows": None,
        "csv_payload": None,
        "csv_accepted": 0,
    }
    context.update(overrides)
    return context


def _settings_context(session: Session, retention_error: str | None = None, proxy_error: str | None = None, proxy_value: str = "") -> dict:
    setting = session.get(AppSetting, 1)
    proxies = session.scalars(select(Proxy).order_by(Proxy.id)).all()
    return {
        "retention_days": setting.retention_days if setting else 30,
        "retention_error": retention_error,
        "proxies": proxies,
        "proxy_error": proxy_error,
        "proxy_value": proxy_value,
        "proxy_label": proxy_label,
    }


def login_redirect(request: Request, exc: LoginRequired):
    nxt = quote(exc.next_path or "/")
    return RedirectResponse(f"/login?next={nxt}", status_code=303)


def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return render(request, "404.html", status_code=404, detail=str(exc.detail))
    if exc.status_code == 403:
        return render(request, "403.html", status_code=403)
    logger.warning("HTTP %s on %s", exc.status_code, request.url.path)
    return render(request, "500.html", status_code=exc.status_code)


def unhandled_error(request: Request, exc: Exception):
    logger.exception("Unhandled error")
    try:
        return render(request, "500.html", status_code=500)
    except Exception:
        logger.exception("Error page failed")
        from fastapi.responses import HTMLResponse

        return HTMLResponse("Something went wrong.", status_code=500)
