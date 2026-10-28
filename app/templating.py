from pathlib import Path

from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.levels import check_level
from app.presentation import format_dt, json_list, profiles_used, screenshot_mark, short_text, status_kind, status_text

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
TEMPLATES.env.globals.update(
    check_level=check_level,
    status_kind=status_kind,
    status_text=status_text,
    short_text=short_text,
    format_dt=format_dt,
    json_list=json_list,
    profiles_used=profiles_used,
    screenshot_mark=screenshot_mark,
    intervals=[1, 5, 15, 30, 60],
    retention_choices=[7, 30, 90],
)
TEMPLATES.env.autoescape = True


def render_template(request, name: str, context: dict, status_code: int = 200):
    payload = dict(context)
    payload["request"] = request
    html = TEMPLATES.env.get_template(name).render(payload)
    return HTMLResponse(html, status_code=status_code)
