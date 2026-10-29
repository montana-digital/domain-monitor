from pathlib import Path

from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.attention import WATCH_INTERVALS
from app.capabilities import browser_on, browser_reason, registration_on
from app.levels import check_level
from app.presentation import (
    format_dt,
    interval_label,
    json_list,
    profiles_used,
    registered_label,
    screenshot_mark,
    short_text,
    status_kind,
    status_text,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
TEMPLATES.env.globals.update(
    browser_on=browser_on,
    browser_reason=browser_reason,
    registration_on=registration_on,
    check_level=check_level,
    status_kind=status_kind,
    status_text=status_text,
    short_text=short_text,
    format_dt=format_dt,
    json_list=json_list,
    profiles_used=profiles_used,
    screenshot_mark=screenshot_mark,
    interval_label=interval_label,
    registered_label=registered_label,
    retention_choices=[7, 30, 90],
    watch_intervals=WATCH_INTERVALS,
)
TEMPLATES.env.autoescape = True


def render_template(request, name: str, context: dict, status_code: int = 200):
    payload = dict(context)
    payload["request"] = request
    html = TEMPLATES.env.get_template(name).render(payload)
    return HTMLResponse(html, status_code=status_code)
