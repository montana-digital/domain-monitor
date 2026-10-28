import asyncio
import contextvars
import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import selectinload

from app.checks.crawl import crawl_monitor
from app.checks.http_check import HttpProber
from app.checks.registration import RegistrationRecord, lookup_registration, rdap_lookup, whois_lookup
from app.checks.runner import execute_check, partial_reason
from app.checks.screenshot import capture_views
from app.checks.types import UNEXPECTED, CheckOutcome, ProxySpec
from app.clock import as_utc, utcnow
from app.config import current
from app import db
from app.db import commit_with_retry
from app.models import CheckAttempt, CheckEvent, CrawlPage, Monitor, Proxy, RegistrationSnapshot, Screenshot
from app.notices import notices
from app.safety import proxy_label

logger = logging.getLogger(__name__)
REGISTRATION_TIMEOUT = 30
LOST_RESULT = "Result could not be stored."
CHECK_LIMIT = 8

_inflight: set[int] = set()
_occupying: set[int] = set()
_reg_inflight: set[int] = set()
_tasks: set[asyncio.Task] = set()
_holding_slot: contextvars.ContextVar[bool] = contextvars.ContextVar("holding_slot", default=False)
_shutting_down = False
_check_sem: asyncio.Semaphore | None = None
_reg_sem: asyncio.Semaphore | None = None
_sem_loop = None
_last_retain = None


def _semaphore(name: str, limit: int) -> asyncio.Semaphore:
    global _check_sem, _reg_sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem_loop is not loop:
        _check_sem = asyncio.Semaphore(CHECK_LIMIT)
        _reg_sem = asyncio.Semaphore(2)
        _sem_loop = loop
    if name == "check":
        assert _check_sem is not None
        return _check_sem
    assert _reg_sem is not None
    return _reg_sem


@dataclass
class MonitorSpec:
    id: int
    hostname: str
    registrable_domain: str
    interval_minutes: int
    screenshot: bool
    crawl: bool
    use_proxy: bool
    mobile: bool
    proxies: list[ProxySpec]


def running_ids() -> set[int]:
    return set(_inflight)


def try_enqueue(monitor_id: int, trigger: str) -> bool:
    if _shutting_down or monitor_id in _inflight:
        return False
    if trigger == "schedule" and len(_occupying) >= CHECK_LIMIT:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _inflight.add(monitor_id)
    _occupying.add(monitor_id)
    task = loop.create_task(_guarded(monitor_id, trigger))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


def begin_shutdown() -> None:
    global _shutting_down
    _shutting_down = True


async def shutdown_checks(timeout: float = 25) -> None:
    begin_shutdown()
    pending = {task for task in _tasks if not task.done()}
    if not pending:
        return
    _done, unfinished = await asyncio.wait(pending, timeout=timeout)
    for task in unfinished:
        task.cancel()
    if unfinished:
        await asyncio.wait(unfinished, timeout=1)


def reset_shutdown() -> None:
    global _shutting_down
    _shutting_down = False


def try_enqueue_registration(monitor_id: int, _trigger: str = "schedule") -> bool:
    if _shutting_down or monitor_id in _reg_inflight:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _reg_inflight.add(monitor_id)
    task = loop.create_task(refresh_registration(monitor_id, already_marked=True))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


def queue_ids(ids: list[int], enqueue) -> None:
    for monitor_id in ids:
        try:
            enqueue(monitor_id, "schedule")
        except Exception:
            logger.exception("Could not queue monitor %s", monitor_id)


def move_next_check_if_sooner(monitor: Monitor, now=None) -> None:
    now = now or utcnow()
    if monitor.last_checked_at is None:
        candidate = now
    else:
        checked = as_utc(monitor.last_checked_at)
        candidate = checked + timedelta(minutes=monitor.interval_minutes)
    current = as_utc(monitor.next_check_at)
    if current is not None and candidate < current:
        monitor.next_check_at = candidate


def registration_failure_banners() -> list[str]:
    if db.SessionLocal is None:
        return []
    with db.SessionLocal() as session:
        latest_id = (
            select(func.max(RegistrationSnapshot.id).label("id"))
            .group_by(RegistrationSnapshot.monitor_id)
            .subquery()
        )
        rows = session.execute(
            select(Monitor.hostname, RegistrationSnapshot.error)
            .join(RegistrationSnapshot, RegistrationSnapshot.monitor_id == Monitor.id)
            .where(
                RegistrationSnapshot.id.in_(select(latest_id.c.id)),
                RegistrationSnapshot.success.is_(False),
            )
            .order_by(Monitor.hostname)
        ).all()
    lines = []
    for hostname, error in rows:
        detail = error or "Registration lookup failed."
        lines.append(f"Registration lookup failed for {hostname}: {detail}")
    return lines


def due_monitor_ids() -> list[int]:
    now = utcnow()
    with _session() as session:
        return list(
            session.scalars(
                select(Monitor.id).where(or_(Monitor.next_check_at.is_(None), Monitor.next_check_at <= now))
            ).all()
        )


def registration_due_ids() -> list[int]:
    cutoff = utcnow() - timedelta(hours=24)
    with _session() as session:
        latest = (
            select(
                RegistrationSnapshot.monitor_id.label("monitor_id"),
                func.max(RegistrationSnapshot.fetched_at).label("fetched_at"),
            )
            .group_by(RegistrationSnapshot.monitor_id)
            .subquery()
        )
        rows = session.execute(
            select(Monitor.id, latest.c.fetched_at).outerjoin(latest, latest.c.monitor_id == Monitor.id)
        ).all()
    due: list[int] = []
    for monitor_id, fetched_at in rows:
        if fetched_at is None or as_utc(fetched_at) < cutoff:
            due.append(monitor_id)
    return due


def stagger_due() -> None:
    now = utcnow()
    with _session() as session:
        due = session.scalars(
            select(Monitor)
            .where(or_(Monitor.next_check_at.is_(None), Monitor.next_check_at <= now))
            .order_by(Monitor.id)
        ).all()
        if not due:
            return
        step = 30 / len(due) if len(due) > 1 else 0
        for index, monitor in enumerate(due):
            monitor.next_check_at = now + timedelta(seconds=index * step)
        commit_with_retry(session)


def recent_problems(session, limit: int = 10) -> list[dict]:
    events = session.scalars(
        select(CheckEvent)
        .options(selectinload(CheckEvent.monitor))
        .order_by(CheckEvent.finished_at.desc())
        .limit(40)
    ).all()
    items: list[dict] = []
    for event in events:
        if event.monitor is None:
            continue
        for line in problem_lines(event):
            items.append(
                {
                    "monitor_id": event.monitor_id,
                    "hostname": event.monitor.hostname,
                    "message": line,
                    "when": event.finished_at or event.started_at,
                }
            )
            if len(items) >= limit:
                return items
    return items


def problem_lines(event: CheckEvent) -> list[str]:
    lines: list[str] = []
    if event.status_code is None and event.error:
        lines.append(event.error)
    elif event.status_code is not None and event.status_code >= 400:
        lines.append(f"HTTP {event.status_code}")
    if event.screenshot_outcome == "failed":
        lines.append(event.screenshot_error or "Screenshot failed")
    if event.crawl_outcome == "failed":
        lines.append(event.crawl_error or "Crawl failed")
    elif event.crawl_outcome == "partial":
        lines.append(event.crawl_error or event.crawl_note or "Crawl incomplete")
    return lines


def maybe_retain() -> None:
    global _last_retain
    now = utcnow()
    if _last_retain is not None and now - _last_retain < timedelta(hours=1):
        return
    retain_screenshots()
    _last_retain = now


def retain_screenshots() -> None:
    from app.models import AppSetting

    cutoff_days = 30
    with _session() as session:
        setting = session.get(AppSetting, 1)
        if setting is not None:
            cutoff_days = setting.retention_days
        cutoff = utcnow() - timedelta(days=cutoff_days)
        shots = session.scalars(
            select(Screenshot).where(Screenshot.created_at < cutoff, Screenshot.path.is_not(None))
        ).all()
        for shot in shots:
            if _unlink(shot.path):
                shot.path = None
                shot.error = "Image removed by retention."
        old_events = session.scalars(
            select(CheckEvent)
            .where(CheckEvent.finished_at.is_not(None), CheckEvent.finished_at < cutoff)
            .options(selectinload(CheckEvent.screenshots))
        ).all()
        for event in old_events:
            if any(shot.path and not _unlink(shot.path) for shot in event.screenshots):
                continue
            session.delete(event)
        commit_with_retry(session)


def delete_monitor(session, monitor: Monitor) -> None:
    shots = session.scalars(
        select(Screenshot).join(CheckEvent).where(CheckEvent.monitor_id == monitor.id)
    ).all()
    for shot in shots:
        _unlink(shot.path)
    session.delete(monitor)
    commit_with_retry(session)


async def run_monitor(monitor_id: int, trigger: str, *, probe=None, capture=None, crawl=None) -> None:
    await _execute_and_save(monitor_id, trigger, probe=probe, capture=capture, crawl=crawl)


async def refresh_registration(monitor_id: int, lookup=None, already_marked: bool = False) -> None:
    if not already_marked:
        if monitor_id in _reg_inflight:
            return
        _reg_inflight.add(monitor_id)
    try:
        async with _semaphore("registration", 2):
            domain = _registrable_domain(monitor_id)
            if domain is None:
                return
            call = lookup or _real_lookup
            try:
                record = await asyncio.wait_for(asyncio.to_thread(call, domain), REGISTRATION_TIMEOUT)
            except TimeoutError:
                logger.warning("Registration timed out for %s", domain)
                record = RegistrationRecord(False, "Registration lookup timed out.")
            except Exception:
                logger.exception("Registration crashed for %s", domain)
                record = RegistrationRecord(False, UNEXPECTED)
            save_snapshot(monitor_id, record)
    finally:
        _reg_inflight.discard(monitor_id)


def save_snapshot(monitor_id: int, record: RegistrationRecord) -> None:
    with _session() as session:
        session.add(
            RegistrationSnapshot(
                monitor_id=monitor_id,
                fetched_at=utcnow(),
                success=record.success,
                error=record.error,
                source=record.source,
                registrar=record.registrar,
                created_date=record.created_date,
                updated_date=record.updated_date,
                expires_date=record.expires_date,
                nameservers=json.dumps(record.nameservers),
                statuses=json.dumps(record.statuses),
            )
        )
        session.flush()
        stale = session.scalars(
            select(RegistrationSnapshot)
            .where(RegistrationSnapshot.monitor_id == monitor_id)
            .order_by(RegistrationSnapshot.fetched_at.desc())
            .offset(30)
        ).all()
        for row in stale:
            session.delete(row)
        commit_with_retry(session)


async def _guarded(monitor_id: int, trigger: str) -> None:
    sem = _semaphore("check", CHECK_LIMIT)
    token = _holding_slot.set(False)
    try:
        await sem.acquire()
        _holding_slot.set(True)
        try:
            await _execute_and_save(monitor_id, trigger)
        finally:
            if _holding_slot.get():
                sem.release()
                _holding_slot.set(False)
    except Exception:
        logger.exception("Monitor %s crashed", monitor_id)
    finally:
        _holding_slot.reset(token)
        _inflight.discard(monitor_id)
        _occupying.discard(monitor_id)


async def _capture_outside_slot(capture, monitor_id: int, url: str, profiles: list[str], proxy_url: str | None):
    if not _holding_slot.get():
        return await capture(url, profiles, proxy_url)
    sem = _semaphore("check", CHECK_LIMIT)
    _occupying.discard(monitor_id)
    sem.release()
    _holding_slot.set(False)
    try:
        return await capture(url, profiles, proxy_url)
    finally:
        _occupying.add(monitor_id)
        await sem.acquire()
        _holding_slot.set(True)


async def _execute_and_save(monitor_id: int, trigger: str, *, probe=None, capture=None, crawl=None) -> None:
    spec = _load_spec(monitor_id)
    if spec is None:
        return
    started = utcnow()
    capture_call = capture or capture_views

    async def releasing_capture(url, profiles, proxy_url):
        return await _capture_outside_slot(capture_call, monitor_id, url, profiles, proxy_url)

    try:
        outcome = await execute_check(
            hostname=spec.hostname,
            registrable_domain=spec.registrable_domain,
            screenshot=spec.screenshot,
            crawl=spec.crawl,
            use_proxy=spec.use_proxy,
            mobile=spec.mobile,
            proxies=spec.proxies if spec.use_proxy else [],
            probe=probe or HttpProber().probe,
            capture=releasing_capture,
            crawl_site=crawl or crawl_monitor,
        )
    except Exception:
        logger.exception("Check failed for %s", spec.hostname)
        _save_crash(spec, trigger, started)
        return
    _save_outcome(spec, trigger, started, outcome)
    logger.info(
        "Checked %s status=%s partial=%s",
        spec.hostname,
        outcome.status_code if outcome.status_code is not None else outcome.error,
        outcome.partial,
    )


def _load_spec(monitor_id: int) -> MonitorSpec | None:
    with _session() as session:
        monitor = session.get(Monitor, monitor_id)
        if monitor is None:
            return None
        proxies = []
        if monitor.use_proxy:
            rows = session.scalars(select(Proxy).where(Proxy.enabled.is_(True)).order_by(Proxy.id)).all()
            proxies = [ProxySpec(row.id, proxy_label(row.url), row.url) for row in rows]
        return MonitorSpec(
            id=monitor.id,
            hostname=monitor.hostname,
            registrable_domain=monitor.registrable_domain,
            interval_minutes=monitor.interval_minutes,
            screenshot=monitor.screenshot_enabled,
            crawl=monitor.crawl_enabled,
            use_proxy=monitor.use_proxy,
            mobile=monitor.mobile_ua,
            proxies=proxies,
        )


def _save_outcome(spec: MonitorSpec, trigger: str, started, outcome: CheckOutcome) -> None:
    for _attempt in range(2):
        written: list[str] = []
        lost = False
        with _session() as session:
            try:
                monitor = session.get(Monitor, spec.id)
                if monitor is None:
                    return
                finished = utcnow()
                event = CheckEvent(
                    monitor_id=monitor.id,
                    trigger=trigger,
                    started_at=started,
                    finished_at=finished,
                    status_code=outcome.status_code,
                    error=outcome.error,
                    partial=outcome.partial,
                    partial_reason=outcome.partial_reason,
                    scheme=outcome.scheme,
                    screenshot_outcome=outcome.screenshot_outcome,
                    screenshot_error=outcome.screenshot_error,
                    crawl_outcome=outcome.crawl_outcome,
                    crawl_error=outcome.crawl_error,
                    crawl_note=outcome.crawl_note,
                    crawl_saved=outcome.crawl_saved,
                    crawl_failed=outcome.crawl_failed,
                )
                session.add(event)
                for attempt in outcome.attempts:
                    session.add(
                        CheckAttempt(
                            event=event,
                            vantage=attempt.vantage,
                            proxy_id=attempt.proxy_id,
                            proxy_label=attempt.proxy_label,
                            profile=attempt.profile,
                            scheme=attempt.scheme,
                            status_code=attempt.status_code,
                            error=attempt.error,
                            latency_ms=attempt.latency_ms,
                            final_url=attempt.final_url,
                            last_modified=attempt.last_modified,
                        )
                    )
                    if attempt.proxy_id is not None:
                        proxy = session.get(Proxy, attempt.proxy_id)
                        if proxy is not None:
                            if attempt.error and attempt.status_code is None:
                                proxy.last_error = attempt.error
                                proxy.last_error_at = finished
                            elif attempt.status_code is not None:
                                proxy.last_error = None
                                proxy.last_error_at = None
                for page in outcome.pages:
                    session.add(
                        CrawlPage(
                            event=event,
                            url=page.url,
                            depth=page.depth,
                            status_code=page.status_code,
                            error=page.error,
                            title=page.title,
                        )
                    )
                session.flush()
                screenshot_outcome = outcome.screenshot_outcome
                screenshot_error = outcome.screenshot_error
                stored_any = False
                write_failed = False
                for shot in outcome.shots:
                    path = None
                    error = shot.error
                    if shot.data:
                        try:
                            path = _write_shot(event.id, shot.viewport, shot.data)
                            written.append(path)
                            stored_any = True
                        except OSError:
                            logger.exception("Could not store a screenshot")
                            write_failed = True
                            error = "Could not write the screenshot file."
                    session.add(
                        Screenshot(
                            event=event,
                            viewport=shot.viewport,
                            path=path,
                            error=error,
                            created_at=finished,
                        )
                    )
                if write_failed:
                    notices.disk_error = "Could not write the screenshot file."
                    if not stored_any:
                        screenshot_outcome = "failed"
                        screenshot_error = "Could not write the screenshot file."
                elif stored_any:
                    notices.disk_error = None
                event.screenshot_outcome = screenshot_outcome
                event.screenshot_error = screenshot_error
                reason = partial_reason(outcome.status_code, screenshot_outcome, outcome.crawl_outcome)
                event.partial = bool(reason)
                event.partial_reason = reason
                monitor.last_checked_at = finished
                monitor.last_status_code = outcome.status_code
                monitor.last_error = outcome.error
                monitor.last_partial = bool(reason)
                monitor.last_partial_reason = reason
                monitor.last_scheme = outcome.scheme
                monitor.next_check_at = finished + timedelta(minutes=monitor.interval_minutes)
                commit_with_retry(session)
                return
            except OperationalError:
                session.rollback()
                for path in written:
                    _unlink(path)
                if _attempt == 1:
                    logger.exception("Could not save the check for %s", spec.hostname)
                    lost = True
                else:
                    time.sleep(0.2)
        if lost:
            _save_lost_outcome(spec, trigger, started, outcome)
            return


def _save_lost_outcome(spec: MonitorSpec, trigger: str, started, outcome: CheckOutcome) -> None:
    finished = utcnow()
    error = LOST_RESULT if not outcome.error else f"{outcome.error} {LOST_RESULT}"
    try:
        with _session() as session:
            monitor = session.get(Monitor, spec.id)
            if monitor is None:
                return
            session.add(
                CheckEvent(
                    monitor_id=monitor.id,
                    trigger=trigger,
                    started_at=started,
                    finished_at=finished,
                    status_code=outcome.status_code,
                    error=error,
                    partial=outcome.partial,
                    partial_reason=outcome.partial_reason,
                    scheme=outcome.scheme,
                    screenshot_outcome=outcome.screenshot_outcome,
                    screenshot_error=outcome.screenshot_error,
                    crawl_outcome=outcome.crawl_outcome,
                    crawl_error=outcome.crawl_error,
                    crawl_note=outcome.crawl_note,
                    crawl_saved=outcome.crawl_saved,
                    crawl_failed=outcome.crawl_failed,
                )
            )
            monitor.last_checked_at = finished
            monitor.last_status_code = outcome.status_code
            monitor.last_error = error
            monitor.last_partial = outcome.partial
            monitor.last_partial_reason = outcome.partial_reason
            monitor.last_scheme = outcome.scheme
            monitor.next_check_at = finished + timedelta(minutes=monitor.interval_minutes)
            commit_with_retry(session)
    except OperationalError:
        logger.exception("Could not record the lost check for %s", spec.hostname)


def _save_crash(spec: MonitorSpec, trigger: str, started) -> None:
    finished = utcnow()
    with _session() as session:
        monitor = session.get(Monitor, spec.id)
        if monitor is None:
            return
        session.add(
            CheckEvent(
                monitor_id=monitor.id,
                trigger=trigger,
                started_at=started,
                finished_at=finished,
                error=UNEXPECTED,
                partial=False,
                screenshot_outcome="skipped",
                crawl_outcome="skipped",
            )
        )
        monitor.last_checked_at = finished
        monitor.last_status_code = None
        monitor.last_error = UNEXPECTED
        monitor.last_partial = False
        monitor.last_partial_reason = None
        monitor.next_check_at = finished + timedelta(minutes=monitor.interval_minutes)
        commit_with_retry(session)


def _registrable_domain(monitor_id: int) -> str | None:
    with _session() as session:
        monitor = session.get(Monitor, monitor_id)
        if monitor is None:
            return None
        return monitor.registrable_domain


def _real_lookup(domain: str) -> RegistrationRecord:
    return lookup_registration(domain, rdap=rdap_lookup, whois_lookup=whois_lookup)


def stored_screenshot_path(stored: str | None) -> Path | None:
    if not stored:
        return None
    root = (current().data_dir / "screenshots").resolve()
    raw = Path(stored)
    if raw.is_absolute():
        try:
            resolved = raw.resolve()
        except OSError:
            return None
    else:
        if raw.name != str(raw) or raw.name in {"", ".", ".."}:
            return None
        resolved = (root / raw.name).resolve()
    if not resolved.is_relative_to(root):
        return None
    return resolved


def _write_shot(event_id: int, viewport: str, data: bytes) -> str:
    directory = current().data_dir / "screenshots"
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{event_id}-{viewport}.jpg"
    (directory / name).write_bytes(data)
    return name


def _unlink(path: str | None) -> bool:
    if not path:
        return True
    target = stored_screenshot_path(path)
    if target is None:
        return True
    try:
        target.unlink(missing_ok=True)
        return True
    except OSError:
        logger.warning("Could not delete screenshot %s", path)
        return False


def _session():
    if db.SessionLocal is None:
        raise RuntimeError("Database is not initialized.")
    return db.SessionLocal()
