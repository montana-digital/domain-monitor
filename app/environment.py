"""Facts about this process, and a direct check of how it reaches the internet.

The network snapshot is remembered in this process for five minutes. It is not
written to SQLite. A restart clears it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import platform
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError, version

import httpx

from app.capabilities import capabilities
from app.checks.agents import DESKTOP_UA, MOBILE_UA
from app.checks.crawl import CRAWL_ACCEPT, CRAWL_TIMEOUT_SECONDS
from app.checks.http_check import (
    PROBE_ACCEPT,
    PROBE_TIMEOUT_SECONDS,
    RETRY_DELAY_SECONDS,
    SCHEME_ORDER,
)
from app.checks.registration import ip_org_lookup
from app.checks.runner import CHECK_DEADLINE_SECONDS, VANTAGE_CAP
from app.checks.screenshot import SCREENSHOT_TIMEOUT_MS
from app.clock import as_utc, utcnow
from app.config import Settings
from app.notices import notices
from app.presentation import MONITOR_POLL_SECONDS, NOTICE_POLL_SECONDS
from app.safety import DNS_TIMEOUT, is_blocked_ip
from app.scheduler import TICK_SECONDS
from app.service import CHECK_LIMIT, HOST_TIMEOUT, REGISTRATION_AGE_HOURS, REGISTRATION_LIMIT

logger = logging.getLogger(__name__)

TRACE_TIMEOUT_SECONDS = 5
CACHE_MINUTES = 5
TRACE_BODY_LIMIT = 8192
ISP_UNAVAILABLE = "Registration lookups are unavailable."
SCREENSHOTS_UNAVAILABLE = "Screenshots are unavailable."
PRIVATE_ADDRESS = "Private address"
NO_ADDRESS = "No address in the trace"
REDIRECT_REFUSED = "Redirect refused"
TIMED_OUT = "Timed out"
CONNECTION_FAILED = "Connection failed"

TraceFetch = Callable[[str], Awaitable["TraceResponse"]]
LocalRoute = Callable[[str], str | None]
OrgLookup = Callable[[str], str | None]
ReverseLookup = Callable[[str], str | None]


@dataclass(frozen=True)
class TraceTarget:
    family: str
    address: str
    url: str


TRACE_TARGETS = (
    TraceTarget("IPv4", "1.1.1.1", "https://1.1.1.1/cdn-cgi/trace"),
    TraceTarget("IPv6", "2606:4700:4700::1111", "https://[2606:4700:4700::1111]/cdn-cgi/trace"),
)


@dataclass
class TraceResponse:
    status_code: int
    body: str
    is_redirect: bool = False


@dataclass
class FamilyStatus:
    family: str
    internet: bool
    public_ip: str | None = None
    local_address: str | None = None
    http_version: str | None = None
    tls_version: str | None = None
    isp: str | None = None
    reverse_name: str | None = None
    error: str | None = None


@dataclass
class NetworkSnapshot:
    families: list[FamilyStatus]
    probed_at: datetime


@dataclass
class EnvironmentFacts:
    version: str
    python_version: str
    operating_system: str
    hostname: str
    listen: str
    data_dir: str
    password_set: str
    https_only: str
    scheduler: str
    last_tick: datetime | None
    registration: str
    browser: str


@dataclass
class NetworkHooks:
    fetch: TraceFetch | None = None
    local_route: LocalRoute | None = None
    org_lookup: OrgLookup | None = None
    reverse_lookup: ReverseLookup | None = None


hooks = NetworkHooks()
_snapshot: NetworkSnapshot | None = None
_lock: asyncio.Lock | None = None
_lock_loop = None


def app_version() -> str:
    try:
        return version("domain-monitor")
    except PackageNotFoundError:
        return "unknown"


def trace_user_agent() -> str:
    return f"DomainMonitor/{app_version()} (environment check)"


def environment_facts(settings: Settings) -> EnvironmentFacts:
    if capabilities.registration:
        registration = "On"
    else:
        registration = capabilities.registration_reason or ISP_UNAVAILABLE
    if capabilities.browser:
        browser = "On"
    else:
        browser = capabilities.browser_reason or SCREENSHOTS_UNAVAILABLE
    return EnvironmentFacts(
        version=app_version(),
        python_version=platform.python_version(),
        operating_system=f"{platform.system()} {platform.release()} {platform.machine()}".strip(),
        hostname=socket.gethostname(),
        listen=f"{settings.host}:{settings.port}",
        data_dir=str(settings.data_dir),
        password_set="Set" if settings.password else "Not set",
        https_only="On" if settings.https_only else "Off",
        scheduler="On" if notices.scheduler_enabled else "Off",
        last_tick=notices.last_tick,
        registration=registration,
        browser=browser,
    )


def scrape_rows() -> list[tuple[str, str]]:
    schemes = ", then ".join(scheme.upper() for scheme in SCHEME_ORDER)
    return [
        ("Desktop user agent", DESKTOP_UA),
        ("Mobile user agent", MOBILE_UA),
        ("Crawl user agent", DESKTOP_UA),
        ("Accept", PROBE_ACCEPT),
        ("Crawl Accept", CRAWL_ACCEPT),
        ("Scheme order", schemes),
        ("Retry", f"Once, after {RETRY_DELAY_SECONDS:g} seconds"),
        ("Probe timeout", f"{PROBE_TIMEOUT_SECONDS:g} seconds"),
        ("Crawl timeout", f"{CRAWL_TIMEOUT_SECONDS:g} seconds"),
        ("Screenshot navigation", f"{SCREENSHOT_TIMEOUT_MS // 1000:g} seconds"),
        ("Check budget", f"{CHECK_DEADLINE_SECONDS:g} seconds"),
        ("Vantages", str(VANTAGE_CAP)),
        ("Check slots", str(CHECK_LIMIT)),
        ("Registration slots", str(REGISTRATION_LIMIT)),
        ("Scheduler tick", f"{TICK_SECONDS:g} seconds"),
        ("Registration age", f"{REGISTRATION_AGE_HOURS:g} hours"),
        ("Monitor list poll", f"{MONITOR_POLL_SECONDS:g} seconds"),
        ("Notices poll", f"{NOTICE_POLL_SECONDS:g} seconds"),
    ]


def reset_network() -> None:
    global _snapshot
    _snapshot = None
    hooks.fetch = None
    hooks.local_route = None
    hooks.org_lookup = None
    hooks.reverse_lookup = None


def local_route(address: str) -> str | None:
    """Return the local address the OS would use. The UDP connect sends no payload."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        sock.connect((address, 443))
        name = sock.getsockname()
    except OSError:
        return None
    finally:
        sock.close()
    if not name:
        return None
    return name[0] or None


def reverse_name(address: str) -> str | None:
    try:
        host, _port = socket.getnameinfo((address, 0), socket.NI_NAMEREQD)
    except OSError:
        return None
    if not host or host == address:
        return None
    return host


def trace_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in body.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        name = key.strip()
        if name and name not in fields:
            fields[name] = value.strip()
    return fields


async def fetch_trace(url: str) -> TraceResponse:
    async with httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(TRACE_TIMEOUT_SECONDS),
        verify=True,
    ) as client:
        response = await client.get(url, headers={"User-Agent": trace_user_agent()})
    body = response.content[:TRACE_BODY_LIMIT].decode("utf-8", errors="replace")
    return TraceResponse(status_code=response.status_code, body=body, is_redirect=response.is_redirect)


def _trace_error(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return TIMED_OUT
    return CONNECTION_FAILED


def _no_local(_address: str) -> str | None:
    return None


def _no_name(_address: str) -> str | None:
    return None


def _active_fetch() -> TraceFetch:
    return hooks.fetch or fetch_trace


def _active_local() -> LocalRoute:
    if hooks.local_route is not None:
        return hooks.local_route
    if hooks.fetch is not None:
        return _no_local
    return local_route


def _active_org() -> OrgLookup:
    if hooks.org_lookup is not None:
        return hooks.org_lookup
    if hooks.fetch is not None:
        return _no_name
    return ip_org_lookup


def _active_reverse() -> ReverseLookup:
    if hooks.reverse_lookup is not None:
        return hooks.reverse_lookup
    if hooks.fetch is not None:
        return _no_name
    return reverse_name


def _lock_for_loop() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    assert _lock is not None
    return _lock


def _cached(now: datetime) -> NetworkSnapshot | None:
    if _snapshot is None or _snapshot.probed_at is None:
        return None
    probed = as_utc(_snapshot.probed_at)
    if probed is None or now - probed >= timedelta(minutes=CACHE_MINUTES):
        return None
    return _snapshot


async def current_network(*, fresh: bool = False) -> NetworkSnapshot:
    now = utcnow()
    if not fresh:
        cached = _cached(now)
        if cached is not None:
            return cached
    async with _lock_for_loop():
        if not fresh:
            cached = _cached(utcnow())
            if cached is not None:
                return cached
        snapshot = await collect_network()
        global _snapshot
        _snapshot = snapshot
        return snapshot


async def collect_network() -> NetworkSnapshot:
    fetch = _active_fetch()
    local_for = _active_local()
    org_lookup = _active_org()
    reverse_lookup = _active_reverse()
    families = await asyncio.gather(
        *[_probe_family(target, fetch, local_for) for target in TRACE_TARGETS]
    )
    await asyncio.gather(*[_lookup_identity(row, org_lookup, reverse_lookup) for row in families])
    return NetworkSnapshot(families=list(families), probed_at=utcnow())


async def _probe_family(target: TraceTarget, fetch: TraceFetch, local_for: LocalRoute) -> FamilyStatus:
    if is_blocked_ip(target.address):
        return FamilyStatus(family=target.family, internet=False, error=PRIVATE_ADDRESS)
    local_address, fetched = await asyncio.gather(
        _local_address(local_for, target.address),
        _fetch_one(fetch, target),
        return_exceptions=True,
    )
    if isinstance(local_address, AssertionError):
        raise local_address
    if isinstance(fetched, AssertionError):
        raise fetched
    if isinstance(local_address, BaseException):
        local_address = None
    if isinstance(fetched, BaseException):
        return FamilyStatus(
            family=target.family,
            internet=False,
            local_address=local_address,
            error=_trace_error(fetched),
        )
    fetched.local_address = local_address
    return fetched


async def _local_address(local_for: LocalRoute, address: str) -> str | None:
    try:
        value = await asyncio.to_thread(local_for, address)
    except AssertionError:
        raise
    except Exception:
        logger.exception("Local route lookup failed")
        return None
    if not isinstance(value, str) or not value:
        return None
    return value


async def _fetch_one(fetch: TraceFetch, target: TraceTarget) -> FamilyStatus:
    if is_blocked_ip(target.address):
        return FamilyStatus(family=target.family, internet=False, error=PRIVATE_ADDRESS)
    try:
        response = await asyncio.wait_for(fetch(target.url), TRACE_TIMEOUT_SECONDS)
    except AssertionError:
        raise
    except Exception as exc:
        return FamilyStatus(family=target.family, internet=False, error=_trace_error(exc))
    return _family_from_response(target.family, response)


def _family_from_response(family: str, response: TraceResponse) -> FamilyStatus:
    if response.is_redirect or response.status_code in {301, 302, 303, 307, 308}:
        return FamilyStatus(family=family, internet=False, error=REDIRECT_REFUSED)
    if response.status_code != 200:
        return FamilyStatus(family=family, internet=False, error=f"HTTP {response.status_code}")
    fields = trace_fields(response.body)
    address = fields.get("ip") or ""
    if not address:
        return FamilyStatus(family=family, internet=False, error=NO_ADDRESS)
    if is_blocked_ip(address):
        return FamilyStatus(family=family, internet=False, error=PRIVATE_ADDRESS)
    return FamilyStatus(
        family=family,
        internet=True,
        public_ip=address,
        http_version=fields.get("http") or None,
        tls_version=fields.get("tls") or None,
    )


async def _lookup_identity(row: FamilyStatus, org_lookup: OrgLookup, reverse_lookup: ReverseLookup) -> None:
    if not row.internet or not row.public_ip or is_blocked_ip(row.public_ip):
        if row.public_ip and is_blocked_ip(row.public_ip):
            row.public_ip = None
            row.internet = False
            row.http_version = None
            row.tls_version = None
            row.error = PRIVATE_ADDRESS
        return
    address = row.public_ip
    row.isp, row.reverse_name = await asyncio.gather(
        _isp(address, org_lookup),
        _reverse(address, reverse_lookup),
    )


async def _isp(address: str, org_lookup: OrgLookup) -> str | None:
    if not capabilities.registration:
        return ISP_UNAVAILABLE
    try:
        return await asyncio.wait_for(asyncio.to_thread(org_lookup, address), HOST_TIMEOUT)
    except TimeoutError:
        return None
    except AssertionError:
        raise
    except Exception:
        logger.exception("ISP lookup failed")
        return None


async def _reverse(address: str, reverse_lookup: ReverseLookup) -> str | None:
    try:
        return await asyncio.wait_for(asyncio.to_thread(reverse_lookup, address), DNS_TIMEOUT)
    except TimeoutError:
        return None
    except AssertionError:
        raise
    except Exception:
        logger.exception("Reverse lookup failed")
        return None
