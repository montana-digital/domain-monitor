import json
from datetime import datetime, timedelta

from app.clock import as_utc, utcnow
from app.models import Monitor

WATCH_INTERVAL = 30
WATCH_INTERVALS = (5, 15, 30, 60, 240)
ROUTINE_4H = 4 * 60
ROUTINE_24H = 24 * 60
ROUTINE_WEEK = 7 * 24 * 60
DEFAULT_WATCH_DAYS = 30
MIN_WATCH_DAYS = 1
MAX_WATCH_DAYS = 365
MONITOR_PAGE_SIZE = 10
SCHEMA_VERSION = 1

LADDER_INTERVALS = {WATCH_INTERVAL, ROUTINE_4H, ROUTINE_24H, ROUTINE_WEEK}


def parse_watch_days(value) -> tuple[int, str | None]:
    raw = str(value or "").strip()
    if raw == "":
        return DEFAULT_WATCH_DAYS, None
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WATCH_DAYS, "Watch duration must be between 1 and 365 days."
    if days < MIN_WATCH_DAYS or days > MAX_WATCH_DAYS:
        return DEFAULT_WATCH_DAYS, "Watch duration must be between 1 and 365 days."
    return days, None


def parse_watch_interval(value) -> tuple[int, str | None]:
    raw = str("" if value is None else value).strip()
    if raw == "":
        return WATCH_INTERVAL, None
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return WATCH_INTERVAL, _watch_interval_error()
    if minutes not in WATCH_INTERVALS:
        return WATCH_INTERVAL, _watch_interval_error()
    return minutes, None


def _watch_interval_error() -> str:
    choices = ", ".join(str(minutes) for minutes in WATCH_INTERVALS)
    return f"Check interval must be one of {choices} minutes."


def routine_interval(since: datetime, now: datetime) -> int:
    age = now - since
    if age >= timedelta(days=90):
        return ROUTINE_WEEK
    if age >= timedelta(days=30):
        return ROUTINE_24H
    return ROUTINE_4H


def move_next_check_if_sooner(monitor: Monitor, now: datetime | None = None) -> None:
    now = now or utcnow()
    if monitor.last_checked_at is None:
        candidate = now
    else:
        checked = as_utc(monitor.last_checked_at)
        candidate = checked + timedelta(minutes=monitor.interval_minutes)
    current = as_utc(monitor.next_check_at)
    if current is not None and candidate < current:
        monitor.next_check_at = candidate


def defer_next_check(monitor: Monitor, now: datetime | None = None) -> None:
    now = now or utcnow()
    checked = as_utc(monitor.last_checked_at)
    if checked is None:
        monitor.next_check_at = now
    else:
        monitor.next_check_at = checked + timedelta(minutes=monitor.interval_minutes)


def assign_attention(
    monitor: Monitor,
    *,
    watching: bool,
    days: int,
    now: datetime,
    creating: bool = False,
    interval: int | None = None,
) -> None:
    monitor.watch_days = days
    if monitor.quiet_since is None:
        monitor.quiet_since = now
    if watching:
        chosen = interval if interval in WATCH_INTERVALS else WATCH_INTERVAL
        previous = monitor.interval_minutes
        monitor.attention = "watch"
        monitor.watch_until = now + timedelta(days=days)
        monitor.interval_minutes = chosen
        if not creating:
            if chosen < previous:
                move_next_check_if_sooner(monitor, now)
            elif chosen > previous:
                defer_next_check(monitor, now)
        return
    leaving_watch = monitor.attention == "watch"
    monitor.attention = "routine"
    if creating or leaving_watch:
        monitor.routine_since = now
        monitor.interval_minutes = ROUTINE_4H
        if not creating:
            defer_next_check(monitor, now)


def note_activity(monitor: Monitor, now: datetime) -> None:
    monitor.quiet_since = now
    if monitor.attention == "watch":
        days = monitor.watch_days or DEFAULT_WATCH_DAYS
        monitor.watch_until = now + timedelta(days=days)
        if monitor.interval_minutes not in WATCH_INTERVALS:
            monitor.interval_minutes = WATCH_INTERVAL
        return
    monitor.attention = "routine"
    monitor.routine_since = now
    monitor.interval_minutes = ROUTINE_4H


def status_changed(old_code, old_error, new_code, new_error, had_check: bool) -> bool:
    if not had_check:
        return False
    if old_code != new_code:
        return True
    if old_code is None and (old_error or "") != (new_error or ""):
        return True
    return False


def step_monitor(monitor: Monitor, now: datetime) -> bool:
    if monitor.attention == "watch":
        changed = False
        until = as_utc(monitor.watch_until)
        if until is None:
            monitor.watch_until = now + timedelta(days=monitor.watch_days or DEFAULT_WATCH_DAYS)
            changed = True
        elif now >= until:
            monitor.attention = "routine"
            monitor.routine_since = now
            monitor.interval_minutes = ROUTINE_4H
            defer_next_check(monitor, now)
            return True
        if monitor.interval_minutes not in WATCH_INTERVALS:
            previous = monitor.interval_minutes
            monitor.interval_minutes = WATCH_INTERVAL
            if WATCH_INTERVAL < previous:
                move_next_check_if_sooner(monitor, now)
            elif WATCH_INTERVAL > previous:
                defer_next_check(monitor, now)
            changed = True
        return changed
    since = as_utc(monitor.routine_since) or as_utc(monitor.quiet_since) or as_utc(monitor.created_at) or now
    desired = routine_interval(since, now)
    if monitor.attention == "routine" and monitor.interval_minutes == desired:
        return False
    monitor.attention = "routine"
    if monitor.routine_since is None:
        monitor.routine_since = since
    previous = monitor.interval_minutes
    monitor.interval_minutes = desired
    if desired != previous:
        defer_next_check(monitor, now)
    return True


def effective_registered(record) -> bool | None:
    if record.registered is not None:
        return record.registered
    if record.success:
        return True
    return None


def _iso(value) -> str:
    moment = as_utc(value)
    if moment is None:
        return ""
    return moment.isoformat()


def _statuses(value) -> tuple:
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ()
    if not isinstance(value, list):
        return ()
    return tuple(sorted(str(item) for item in value))


def registry_changed(previous, record, previous_host: str | None) -> bool:
    if previous is None and not previous_host:
        return False
    new_registered = effective_registered(record)
    new_host = record.host_org or None
    if new_registered is None:
        return bool(new_host) and new_host != (previous_host or "")
    if previous is None:
        old_registered = None
        old_registrar = ""
        old_created = old_updated = old_expires = ""
        old_statuses: tuple = ()
    else:
        old_registered = True if previous.registered is None and previous.success else previous.registered
        old_registrar = previous.registrar or ""
        old_created = _iso(previous.created_date)
        old_updated = _iso(previous.updated_date)
        old_expires = _iso(previous.expires_date)
        old_statuses = _statuses(previous.statuses)
    old_host = previous_host or ""
    new_host_value = new_host if new_host else old_host
    old_sig = (old_registered, old_registrar, old_created, old_updated, old_expires, old_statuses, old_host)
    new_sig = (
        new_registered,
        (record.registrar or "") if new_registered else "",
        _iso(record.created_date),
        _iso(record.updated_date),
        _iso(record.expires_date),
        _statuses(record.statuses),
        new_host_value,
    )
    return old_sig != new_sig
