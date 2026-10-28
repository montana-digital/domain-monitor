import asyncio
import logging
import os
from urllib.parse import urlsplit, unquote

from app.checks.agents import user_agent
from app.checks.pin_proxy import start_pin_proxy
from app.checks.types import UNEXPECTED, Shot
from app.notices import notices
from app.safety import public_hostname_error

logger = logging.getLogger(__name__)

_playwright = None
_browser = None
_capture_lock: asyncio.Lock | None = None
_capture_loop = None
_VIEWPORTS = {
    "desktop": {"width": 1280, "height": 800},
    "mobile": {"width": 390, "height": 844},
}


def chromium_args() -> list[str]:
    args = ["--disable-dev-shm-usage"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")
    return args


def _capture_lock_for_loop() -> asyncio.Lock:
    global _capture_lock, _capture_loop
    loop = asyncio.get_running_loop()
    if _capture_loop is not loop:
        _capture_lock = asyncio.Lock()
        _capture_loop = loop
    assert _capture_lock is not None
    return _capture_lock


async def blocked_navigation(url: str, cache: dict[str, str | None]) -> str | None:
    host = urlsplit(url).hostname
    if not host:
        return None
    if host not in cache:
        cache[host] = await public_hostname_error(host)
    return cache[host]


async def capture_views(url: str, profiles: list[str], proxy_url: str | None) -> list[Shot]:
    async with _capture_lock_for_loop():
        host = urlsplit(url).hostname or ""
        blocked = await public_hostname_error(host)
        if blocked:
            return [Shot(profile, error=blocked) for profile in profiles]
        try:
            browser = await _launch()
        except Exception as exc:
            logger.exception("Could not launch the browser")
            message = _browser_message(exc)
            notices.browser_error = message
            return [Shot(profile, error=f"Browser unavailable: {message}") for profile in profiles]
        notices.browser_error = None
        pin_server = None
        proxy_config = _playwright_proxy(proxy_url)
        try:
            if proxy_config is None:
                pin_server, pin_port = await start_pin_proxy()
                proxy_config = {"server": f"http://127.0.0.1:{pin_port}"}
            shots: list[Shot] = []
            for profile in profiles:
                shots.append(await _one(browser, url, profile, proxy_config))
            return shots
        finally:
            if pin_server is not None:
                pin_server.close()
                await pin_server.wait_closed()


async def shutdown_browser() -> None:
    global _browser, _playwright
    browser = _browser
    playwright = _playwright
    _browser = None
    _playwright = None
    if browser is not None:
        try:
            await browser.close()
        except Exception:
            logger.exception("Could not close the browser")
    if playwright is not None:
        try:
            await playwright.stop()
        except Exception:
            logger.exception("Could not stop Playwright")


async def _launch():
    global _browser, _playwright
    if _browser is not None:
        if _browser.is_connected():
            return _browser
        try:
            await _browser.close()
        except Exception:
            logger.exception("Could not close a disconnected browser")
        _browser = None
    from playwright.async_api import async_playwright

    if _playwright is None:
        _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(headless=True, args=chromium_args())
    return _browser


async def _one(browser, url: str, profile: str, proxy_config: dict | None) -> Shot:
    context = None
    cache: dict[str, str | None] = {}
    document_blocked = False
    try:
        context = await browser.new_context(
            user_agent=user_agent(profile),
            viewport=_VIEWPORTS.get(profile, _VIEWPORTS["desktop"]),
            ignore_https_errors=True,
            proxy=proxy_config,
        )
        page = await context.new_page()

        async def guard(route) -> None:
            nonlocal document_blocked
            reason = await blocked_navigation(route.request.url, cache)
            if reason:
                if route.request.resource_type == "document":
                    document_blocked = True
                await route.abort()
                return
            await route.continue_()

        await page.route("**/*", guard)
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if document_blocked:
            return Shot(profile, error="Private address")
        data = await page.screenshot(type="jpeg", quality=60, full_page=False)
        return Shot(profile, data=data)
    except Exception as exc:
        if document_blocked:
            return Shot(profile, error="Private address")
        logger.exception("Screenshot failed for %s", url)
        message = "Timed out" if exc.__class__.__name__ == "TimeoutError" else UNEXPECTED
        if message == UNEXPECTED and "timeout" in str(exc).lower():
            message = "Timed out"
        return Shot(profile, error=message)
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                logger.exception("Could not close the browser context")


def _playwright_proxy(proxy_url: str | None) -> dict | None:
    if not proxy_url:
        return None
    parts = urlsplit(proxy_url)
    port = f":{parts.port}" if parts.port else ""
    config = {"server": f"{parts.scheme}://{parts.hostname}{port}"}
    if parts.username:
        config["username"] = unquote(parts.username)
    if parts.password:
        config["password"] = unquote(parts.password)
    return config


def _browser_message(exc: Exception) -> str:
    text = str(exc).strip()
    if not text or "traceback" in text.lower():
        return "The browser could not be started."
    return text[:180]
