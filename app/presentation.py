import json
from datetime import datetime, timedelta

from app.clock import as_utc, utcnow
from app.models import Monitor


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


def section_for(path: str) -> str:
    if path.startswith("/settings"):
        return "settings"
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
