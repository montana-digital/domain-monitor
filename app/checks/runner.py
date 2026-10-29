import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from app.checks.types import (
    UNEXPECTED,
    AttemptRecord,
    CheckOutcome,
    CrawlResult,
    HttpResult,
    ProxySpec,
    Shot,
)

logger = logging.getLogger(__name__)
VANTAGE_CAP = 6
CHECK_DEADLINE_SECONDS = 180
Probe = Callable[[str, str, str | None], Awaitable[HttpResult]]
Capture = Callable[[str, list[str], str | None], Awaitable[list[Shot]]]
Crawl = Callable[[str, str, str | None, float], Awaitable[CrawlResult]]


def build_vantages(mobile: bool, use_proxy: bool, proxies: list[ProxySpec]) -> list[tuple[str, int | None, str | None, str]]:
    vantages: list[tuple[str, int | None, str | None, str]] = [("Direct desktop", None, None, "desktop")]
    if mobile:
        vantages.append(("Direct mobile", None, None, "mobile"))
    if use_proxy:
        for proxy in proxies:
            vantages.append((f"Proxy desktop ({proxy.label})", proxy.id, proxy.url, "desktop"))
            if mobile:
                vantages.append((f"Proxy mobile ({proxy.label})", proxy.id, proxy.url, "mobile"))
    return vantages[:VANTAGE_CAP]


def partial_reason(status_code: int | None, screenshot_outcome: str, crawl_outcome: str) -> str | None:
    if status_code is None:
        return None
    reasons = []
    if screenshot_outcome == "failed":
        reasons.append("Screenshot failed")
    if crawl_outcome == "failed":
        reasons.append("Crawl failed")
    elif crawl_outcome == "partial":
        reasons.append("Crawl incomplete")
    return ", ".join(reasons) or None


async def execute_check(
    *,
    hostname: str,
    registrable_domain: str,
    screenshot: bool,
    crawl: bool,
    use_proxy: bool,
    mobile: bool,
    proxies: list[ProxySpec],
    probe: Probe,
    capture: Capture,
    crawl_site: Crawl,
    deadline_seconds: float = CHECK_DEADLINE_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> CheckOutcome:
    deadline = monotonic() + deadline_seconds
    attempts: list[AttemptRecord] = []
    expired = False
    for vantage, proxy_id, proxy_url, profile in build_vantages(mobile, use_proxy, proxies):
        remaining = deadline - monotonic()
        if remaining <= 0:
            expired = True
            break
        try:
            result = await asyncio.wait_for(probe(hostname, profile, proxy_url), remaining)
        except TimeoutError:
            expired = True
            attempts.append(
                _attempt(vantage, proxy_id, proxy_url, profile, HttpResult(error="Check timed out"))
            )
            break
        except Exception:
            logger.exception("Probe crashed for %s", hostname)
            result = HttpResult(error=UNEXPECTED)
        attempts.append(_attempt(vantage, proxy_id, proxy_url, profile, result))

    status_code, error, scheme = _display_status(attempts)
    target = _capture_target(attempts)
    shots: list[Shot] = []
    screenshot_outcome = "skipped"
    screenshot_error = None
    if screenshot:
        remaining = deadline - monotonic()
        if expired or remaining <= 0:
            screenshot_outcome = "failed"
            screenshot_error = "Check timed out"
        elif target is None or not target.scheme:
            screenshot_outcome = "failed"
            screenshot_error = "No HTTP response to capture"
        else:
            profiles = ["desktop"]
            if mobile:
                profiles.append("mobile")
            url = f"{target.scheme}://{hostname}/"
            timed_out = False
            try:
                shots = await asyncio.wait_for(capture(url, profiles, target.proxy_url), remaining)
            except TimeoutError:
                expired = True
                timed_out = True
                screenshot_outcome = "failed"
                screenshot_error = "Check timed out"
            except Exception:
                logger.exception("Screenshot crashed for %s", hostname)
                shots = [Shot(profile, error=UNEXPECTED) for profile in profiles]
            if not timed_out and not shots:
                screenshot_outcome = "failed"
                screenshot_error = UNEXPECTED
            elif not timed_out and any(shot.error for shot in shots):
                screenshot_outcome = "failed"
                screenshot_error = "; ".join(shot.error for shot in shots if shot.error)
            elif not timed_out:
                screenshot_outcome = "succeeded"

    crawl_outcome = "skipped"
    crawl_error = None
    crawl_note = None
    crawl_saved = 0
    crawl_failed = 0
    pages = []
    if crawl:
        if expired or monotonic() >= deadline:
            crawl_outcome = "failed"
            crawl_error = "Check timed out"
        elif target is None or not target.scheme:
            crawl_outcome = "failed"
            crawl_error = "Homepage unreachable"
        else:
            start_url = f"{target.scheme}://{hostname}/"
            try:
                crawled = await crawl_site(start_url, registrable_domain, target.proxy_url, deadline)
            except Exception:
                logger.exception("Crawl crashed for %s", hostname)
                crawled = CrawlResult("failed", UNEXPECTED)
            crawl_outcome = crawled.outcome
            crawl_error = crawled.error
            crawl_note = crawled.note
            crawl_saved = crawled.saved
            crawl_failed = crawled.failed
            pages = crawled.pages

    reason = partial_reason(status_code, screenshot_outcome, crawl_outcome)
    return CheckOutcome(
        status_code=status_code,
        error=error,
        scheme=scheme,
        partial=bool(reason),
        partial_reason=reason,
        attempts=attempts,
        screenshot_outcome=screenshot_outcome,
        screenshot_error=screenshot_error,
        shots=shots,
        crawl_outcome=crawl_outcome,
        crawl_error=crawl_error,
        crawl_note=crawl_note,
        crawl_saved=crawl_saved,
        crawl_failed=crawl_failed,
        pages=pages,
    )


def _attempt(vantage: str, proxy_id: int | None, proxy_url: str | None, profile: str, result: HttpResult) -> AttemptRecord:
    return AttemptRecord(
        vantage=vantage,
        proxy_id=proxy_id,
        proxy_label=_label_from_vantage(vantage, proxy_id),
        proxy_url=proxy_url,
        profile=profile,
        scheme=result.scheme,
        status_code=result.status_code,
        error=result.error,
        latency_ms=result.latency_ms,
        final_url=result.final_url,
        last_modified=result.last_modified,
    )


def _display_status(attempts: list[AttemptRecord]) -> tuple[int | None, str | None, str | None]:
    direct = next((item for item in attempts if item.vantage == "Direct desktop"), None)
    success = next((item for item in attempts if item.status_code is not None and 200 <= item.status_code < 300), None)
    if success is not None:
        return success.status_code, None, success.scheme
    if direct is None:
        return None, "No check was attempted.", None
    if direct.status_code is not None:
        return direct.status_code, None, direct.scheme
    return None, direct.error or "Check failed.", direct.scheme


def _capture_target(attempts: list[AttemptRecord]) -> AttemptRecord | None:
    success = next((item for item in attempts if item.status_code is not None and 200 <= item.status_code < 300), None)
    if success is not None:
        return success
    direct = next((item for item in attempts if item.vantage == "Direct desktop"), None)
    if direct is not None and direct.status_code is not None:
        return direct
    return next((item for item in attempts if item.status_code is not None), None)


def _label_from_vantage(vantage: str, proxy_id: int | None) -> str | None:
    if proxy_id is None or "(" not in vantage:
        return None
    return vantage[vantage.find("(") + 1 : -1]
