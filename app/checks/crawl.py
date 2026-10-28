import logging
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from app.checks.agents import DESKTOP_UA
from app.checks.http_check import PrivateTarget, classify_http_error
from app.checks.types import CrawlResult, PageRecord
from app.domain_parser import parse_domain
from app.safety import public_hostname_error

logger = logging.getLogger(__name__)
PAGE_CAP = 20
BODY_LIMIT = 2_000_000
REDIRECT_LIMIT = 5
OFF_SITE = "Left the registrable domain."
_REDIRECTS = {301, 302, 303, 307, 308}

Fetch = Callable[[str], Awaitable["FetchResult"]]


class FetchResult:
    def __init__(
        self,
        status_code: int | None = None,
        error: str | None = None,
        content_type: str = "",
        text: str | None = None,
        final_url: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.error = error
        self.content_type = content_type
        self.text = text
        self.final_url = final_url


def parse_robots(text: str) -> list[str]:
    disallows: list[str] = []
    in_star = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            in_star = value == "*"
        elif key == "disallow" and in_star and value:
            disallows.append(value)
    return disallows


def path_allowed(path: str, disallows: list[str]) -> bool:
    for rule in disallows:
        if path.startswith(rule):
            return False
    return True


def normalize_page_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def same_registrable(hostname: str, registrable_domain: str) -> bool:
    parsed = parse_domain(hostname)
    return parsed.error is None and parsed.registrable_domain == registrable_domain


def redirect_location(current_url: str, location: str | None) -> tuple[str | None, str | None]:
    if location is None or not str(location).strip():
        return None, "Redirect loop"
    absolute = urljoin(current_url, str(location).strip())
    parts = urlsplit(absolute)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None, OFF_SITE
    return absolute, None


def extract_links(page_url: str, html: str, registrable_domain: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        absolute = urljoin(page_url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            continue
        if len(absolute) > 2000:
            continue
        if not same_registrable(parts.hostname, registrable_domain):
            continue
        normalized = normalize_page_url(absolute)
        if normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    return found


def title_from(html: str | None) -> str | None:
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    if soup.title is None:
        return None
    text = " ".join(soup.title.get_text().split())
    return text[:200] or None


def _is_html(content_type: str, text: str | None) -> bool:
    lowered = content_type.lower()
    if "html" in lowered or "xml" in lowered:
        return True
    if text and text.lstrip().startswith("<"):
        return True
    return False


def _skip_body(content_type: str) -> bool:
    lowered = content_type.lower()
    return lowered.startswith(("image/", "video/", "audio/", "application/pdf", "application/zip", "application/octet-stream"))


async def crawl_site(
    start_url: str,
    registrable_domain: str,
    *,
    fetch: Fetch,
    deadline: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> CrawlResult:
    notes: list[str] = []
    pages: list[PageRecord] = []
    saved = 0
    failed = 0
    robots_rules: list[str] = []
    robots_ok = False
    blocked = False
    start = normalize_page_url(start_url)
    parts = urlsplit(start)
    robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))

    if monotonic() > deadline:
        return CrawlResult("failed", "Check timed out")

    try:
        robots = await fetch(robots_url)
        if robots.error is None and robots.status_code is not None and robots.status_code < 400 and robots.text is not None:
            robots_rules = parse_robots(robots.text)
            robots_ok = True
        else:
            notes.append("robots.txt could not be fetched; crawl continued.")
    except Exception:
        logger.exception("robots.txt fetch failed for %s", robots_url)
        notes.append("robots.txt could not be fetched; crawl continued.")

    seen: set[str] = set()
    current = [start]
    for depth in range(3):
        batch = current if depth == 0 else current[:PAGE_CAP]
        next_urls: list[str] = []
        for url in batch:
            if monotonic() > deadline:
                notes.append("Crawl stopped because the check timed out.")
                outcome = "partial" if saved else "failed"
                error = None if saved else "Check timed out"
                return CrawlResult(outcome, error, _notes(notes), saved, failed, pages)
            if url in seen:
                continue
            seen.add(url)
            path = urlsplit(url).path or "/"
            if depth > 0 and robots_ok and not path_allowed(path, robots_rules):
                blocked = True
                continue
            try:
                result = await fetch(url)
            except Exception:
                logger.exception("Crawl fetch failed for %s", url)
                result = FetchResult(error="Check failed: unexpected error")
            title = title_from(result.text)
            pages.append(PageRecord(url, depth, result.status_code, result.error, title))
            if result.error or result.status_code is None:
                failed += 1
                if depth == 0:
                    return CrawlResult(
                        "failed",
                        result.error or "Homepage unreachable",
                        _notes(notes),
                        saved,
                        failed,
                        pages,
                    )
                continue
            saved += 1
            if depth < 2 and _is_html(result.content_type, result.text):
                for link in extract_links(result.final_url or url, result.text or "", registrable_domain):
                    if link not in seen:
                        next_urls.append(link)
        current = _dedupe(next_urls)
    if blocked:
        notes.append("robots.txt disallows some paths.")
    outcome = "partial" if failed else "succeeded"
    return CrawlResult(outcome, None, _notes(notes), saved, failed, pages)


async def _cached_block(host: str, cache: dict[str, str | None]) -> str | None:
    if host not in cache:
        cache[host] = await public_hostname_error(host)
    return cache[host]


async def fetch_on_site(
    client: httpx.AsyncClient,
    url: str,
    registrable_domain: str,
    cache: dict[str, str | None],
) -> FetchResult:
    current = url
    for hop in range(REDIRECT_LIMIT + 1):
        host = urlsplit(current).hostname
        if not host:
            return FetchResult(error="Private address")
        blocked = await _cached_block(host, cache)
        if blocked:
            return FetchResult(error=blocked)
        try:
            response = await client.get(
                current,
                headers={"User-Agent": DESKTOP_UA, "Accept": "text/html,*/*;q=0.8"},
            )
        except Exception as exc:
            message = classify_http_error(exc)
            if message == "Check failed: unexpected error":
                logger.exception("Crawl request failed for %s", current)
            return FetchResult(error=message)
        if response.status_code in _REDIRECTS:
            if hop == REDIRECT_LIMIT:
                return FetchResult(status_code=response.status_code, error="Redirect loop", final_url=str(response.url))
            nxt, error = redirect_location(current, response.headers.get("location"))
            if error or not nxt:
                return FetchResult(status_code=response.status_code, error=error or OFF_SITE, final_url=str(response.url))
            next_host = urlsplit(nxt).hostname or ""
            blocked = await _cached_block(next_host, cache)
            if blocked:
                return FetchResult(status_code=response.status_code, error=blocked, final_url=str(response.url))
            if not same_registrable(next_host, registrable_domain):
                return FetchResult(status_code=response.status_code, error=OFF_SITE, final_url=str(response.url))
            current = nxt
            continue
        content_type = response.headers.get("content-type", "")
        text = None
        if not _skip_body(content_type):
            text = response.content[:BODY_LIMIT].decode(response.encoding or "utf-8", errors="replace")
        return FetchResult(
            status_code=response.status_code,
            content_type=content_type,
            text=text,
            final_url=str(response.url),
        )
    return FetchResult(error="Redirect loop")


async def crawl_monitor(
    start_url: str,
    registrable_domain: str,
    proxy_url: str | None,
    deadline: float,
) -> CrawlResult:
    cache: dict[str, str | None] = {}

    async def reject_private(request: httpx.Request) -> None:
        host = request.url.host
        if not host:
            return
        blocked = await _cached_block(host, cache)
        if blocked:
            raise PrivateTarget(blocked)

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(10.0),
        trust_env=False,
        proxy=proxy_url,
        event_hooks={"request": [reject_private]},
    ) as client:

        async def fetch(url: str) -> FetchResult:
            return await fetch_on_site(client, url, registrable_domain, cache)

        return await crawl_site(
            start_url,
            registrable_domain,
            fetch=fetch,
            deadline=deadline,
        )


def _notes(notes: list[str]) -> str | None:
    cleaned = [note for note in notes if note]
    return " ".join(cleaned) or None


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered
