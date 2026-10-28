import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from app.checks.agents import user_agent
from app.checks.connect import open_client, pin_host
from app.checks.types import HttpResult

logger = logging.getLogger(__name__)
RETRY_STATUSES = {429, 503}
Attempt = Callable[[str], Awaitable[HttpResult]]


class PrivateTarget(Exception):
    pass


def needs_retry(result: HttpResult) -> bool:
    if result.status_code in RETRY_STATUSES:
        return True
    return result.status_code is None and bool(result.error)


async def probe_hostname(hostname: str, *, attempt: Attempt, sleep: Callable[[float], Awaitable[None]]) -> HttpResult:
    https_result = await _once_or_twice("https", attempt, sleep)
    if https_result.status_code is not None:
        return https_result
    http_result = await _once_or_twice("http", attempt, sleep)
    if http_result.status_code is not None:
        return http_result
    if https_result.error == "TLS failed":
        return https_result
    return http_result


async def _once_or_twice(scheme: str, attempt: Attempt, sleep: Callable[[float], Awaitable[None]]) -> HttpResult:
    result = await attempt(scheme)
    if needs_retry(result):
        await sleep(2)
        result = await attempt(scheme)
    return result


def classify_http_error(exc: Exception) -> str:
    if isinstance(exc, PrivateTarget):
        return str(exc) or "Private address"
    if isinstance(exc, httpx.TimeoutException):
        return "Timed out"
    if isinstance(exc, httpx.TooManyRedirects):
        return "Redirect loop"
    text = str(exc).lower()
    if "private address" in text:
        return "Private address"
    if isinstance(exc, httpx.ProxyError):
        return "Proxy connection failed"
    if isinstance(exc, httpx.ConnectError):
        if any(word in text for word in ("certificate", "tls", "ssl", "handshake")):
            return "TLS failed"
        if any(word in text for word in ("getaddrinfo", "name or service", "nodename", "temporary failure in name", "dns")):
            return "DNS failed"
        return "Connection failed"
    if isinstance(exc, httpx.TransportError):
        return "Connection failed"
    return "Check failed: unexpected error"


class HttpProber:
    async def probe(self, hostname: str, profile: str, proxy_url: str | None) -> HttpResult:
        blocked = await pin_host(hostname)
        if blocked:
            return HttpResult(error=blocked)
        agent = user_agent(profile)

        async def attempt(scheme: str) -> HttpResult:
            return await send_once(f"{scheme}://{hostname}/", agent, proxy_url, scheme)

        return await probe_hostname(hostname, attempt=attempt, sleep=_sleep)


async def send_once(url: str, agent: str, proxy_url: str | None, scheme: str) -> HttpResult:
    started = time.monotonic()

    async def reject_private(request: httpx.Request) -> None:
        host = request.url.host
        if not host:
            return
        error = await pin_host(host)
        if error:
            raise PrivateTarget(error)

    try:
        async with open_client(
            follow_redirects=True,
            max_redirects=10,
            timeout=httpx.Timeout(20.0),
            proxy=proxy_url,
            event_hooks={"request": [reject_private]},
        ) as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": agent,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                },
            )
        elapsed = int((time.monotonic() - started) * 1000)
        return HttpResult(
            status_code=response.status_code,
            scheme=scheme,
            final_url=str(response.url),
            latency_ms=elapsed,
            last_modified=response.headers.get("last-modified"),
        )
    except Exception as exc:
        message = classify_http_error(exc)
        if message == "Check failed: unexpected error":
            logger.exception("HTTP probe failed for %s", url)
        elapsed = int((time.monotonic() - started) * 1000)
        return HttpResult(error=message, scheme=scheme, latency_ms=elapsed)


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
