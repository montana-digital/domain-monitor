import json
from datetime import datetime, timedelta

from app.clock import as_utc, utcnow
from app.models import Monitor

MONITOR_POLL_SECONDS = 15
NOTICE_POLL_SECONDS = 30


def status_kind(code: int | None, error: str | None, checked_at: datetime | None) -> str:
    if checked_at is None and code is None and not error:
        return "pending"
    if code is None:
        return "bad"
    if 200 <= code < 300:
        return "ok"
    if 300 <= code < 400:
        return "warn"
    return "bad"


def status_text(code: int | None, error: str | None, checked_at: datetime | None) -> str:
    if checked_at is None and code is None and not error:
        return "Pending"
    if code is None:
        return error or "Failed"
    return str(code)


def short_text(text: str | None, limit: int = 80) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def format_dt(value: datetime | None) -> str:
    moment = as_utc(value)
    if moment is None:
        return "—"
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def json_list(text: str | None) -> list:
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def is_overdue(monitor: Monitor, now: datetime | None = None, checking: bool = False) -> bool:
    if checking or monitor.last_checked_at is None:
        return False
    moment = now or utcnow()
    due_by = as_utc(monitor.last_checked_at) + timedelta(minutes=monitor.interval_minutes + 1)
    return moment > due_by


def interval_label(minutes: int | None) -> str:
    if minutes == 30:
        return "Every 30 min"
    if minutes == 240:
        return "Every 4 hours"
    if minutes == 1440:
        return "Every 24 hours"
    if minutes == 10080:
        return "Weekly"
    if minutes is None:
        return "—"
    if minutes % 1440 == 0 and minutes:
        days = minutes // 1440
        return f"Every {days} day" if days == 1 else f"Every {days} days"
    if minutes % 60 == 0 and minutes:
        hours = minutes // 60
        return f"Every {hours} hour" if hours == 1 else f"Every {hours} hours"
    unit = "minute" if minutes == 1 else "minutes"
    return f"Every {minutes} {unit}"


def registered_label(value: bool | None) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "—"


def debug_severity(code: int | None, error: str | None, checked_at: datetime | None) -> str | None:
    kind = status_kind(code, error, checked_at)
    if kind == "bad":
        return "Serious"
    if kind == "warn":
        return "Warning"
    return None


def section_for(path: str) -> str:
    if path.startswith("/settings"):
        return "settings"
    if path.startswith("/about"):
        return "about"
    if path.startswith("/debug"):
        return "debug"
    if path.startswith("/domains/new") or path == "/domains" or path.startswith("/domains/csv"):
        return "add"
    if path == "/" or path.startswith("/domains/"):
        return "monitors"
    return ""


def safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def screenshot_mark(outcome: str | None, screenshots) -> str:
    if outcome == "succeeded" and any(getattr(item, "path", None) for item in screenshots or []):
        return "saved"
    if outcome == "succeeded":
        return "removed"
    return "missing"


def profiles_used(attempts, screenshots) -> list[str]:
    values: list[str] = []
    for item in attempts or []:
        profile = getattr(item, "profile", None)
        if profile:
            values.append(profile)
    for item in screenshots or []:
        viewport = getattr(item, "viewport", None)
        if viewport:
            values.append(viewport)
    return [name for name in ("desktop", "mobile") if name in values]
