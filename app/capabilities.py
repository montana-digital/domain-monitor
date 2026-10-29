"""Features that stay off when a dependency is missing.

Registration needs ``whoisit`` and ``whois`` plus one RDAP bootstrap.
Screenshots need Playwright and a Chromium binary under the Playwright
browsers directory. The core HTTP checks do not use either one.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

RDAP_PROBE_SECONDS = 15
CHROMIUM_EXECUTABLES = (
    Path("chrome-linux64") / "chrome",
    Path("chrome-linux") / "chrome",
    Path("chrome-win") / "chrome.exe",
)
REGISTRATION_MISSING = "Registration libraries are not installed."
REGISTRATION_UNREACHABLE = "RDAP could not be reached."
PLAYWRIGHT_MISSING = "Playwright is not installed."
CHROMIUM_MISSING = "Chromium is not installed."


@dataclass
class Capabilities:
    registration: bool = False
    browser: bool = False
    registration_reason: str = ""
    browser_reason: str = ""


capabilities = Capabilities()


def registration_available() -> bool:
    return capabilities.registration


def browser_available() -> bool:
    return capabilities.browser


def browser_reason() -> str:
    if capabilities.browser:
        return ""
    return capabilities.browser_reason or "Screenshots are unavailable."


def registration_on() -> bool:
    return capabilities.registration


def browser_on() -> bool:
    return capabilities.browser


def browsers_path() -> Path:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / ".playwright"


def chromium_present(browsers: Path) -> bool:
    if not browsers.is_dir():
        return False
    for revision in browsers.iterdir():
        if not revision.is_dir() or not revision.name.startswith("chromium-"):
            continue
        for relative in CHROMIUM_EXECUTABLES:
            if (revision / relative).is_file():
                return True
    return False


def _can_import(name: str) -> bool:
    try:
        __import__(name)
    except ImportError:
        return False
    return True


def browser_status(*, playwright_ok: bool, chromium_ok: bool) -> tuple[bool, str]:
    if not playwright_ok:
        return False, PLAYWRIGHT_MISSING
    if not chromium_ok:
        return False, CHROMIUM_MISSING
    return True, ""


def detect_browser() -> tuple[bool, str]:
    return browser_status(
        playwright_ok=_can_import("playwright"),
        chromium_ok=chromium_present(browsers_path()),
    )


def probe_registration(
    *,
    imports_ok: bool | None = None,
    bootstrap: Callable[[], None] | None = None,
    timeout: float = RDAP_PROBE_SECONDS,
) -> tuple[bool, str]:
    """Return whether RDAP can be used. A hung bootstrap leaves the feature off."""
    if imports_ok is None:
        imports_ok = _can_import("whoisit") and _can_import("whois")
    if not imports_ok:
        return False, REGISTRATION_MISSING
    if bootstrap is None:
        import whoisit

        bootstrap = whoisit.bootstrap
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            bootstrap()
        except Exception as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive() or not outcome or outcome[0] is not None:
        return False, REGISTRATION_UNREACHABLE
    return True, ""


def refresh_capabilities() -> None:
    from app.notices import notices

    registered, registration_reason = probe_registration()
    browser, reason = detect_browser()
    capabilities.registration = registered
    capabilities.registration_reason = registration_reason
    capabilities.browser = browser
    capabilities.browser_reason = reason
    notices.registration_unavailable = None if registered else "Registration lookups are unavailable."
