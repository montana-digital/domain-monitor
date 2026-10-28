import asyncio
import time

import httpx

from app.checks.crawl import OFF_SITE, crawl_monitor, extract_links, parse_robots, path_allowed
from app.checks.runner import build_vantages
from app.checks.screenshot import blocked_navigation
from app.checks.types import ProxySpec
from app.levels import check_level


def test_check_levels():
    assert check_level(False, False, False, False) == "Status"
    assert check_level(True, False, False, False) == "Visual"
    assert check_level(True, True, False, False) == "Full"
    assert check_level(False, False, True, False) == "Full"
    assert check_level(False, False, False, True) == "Full"
    assert check_level(True, False, True, True) == "Full"


def test_mobile_and_proxy_vantages():
    proxies = [ProxySpec(1, "proxy.example:8080", "http://proxy.example:8080")]
    vantages = build_vantages(True, True, proxies)
    assert [item[0] for item in vantages] == [
        "Direct desktop",
        "Direct mobile",
        "Proxy desktop (proxy.example:8080)",
        "Proxy mobile (proxy.example:8080)",
    ]


def test_crawl_stays_on_the_registrable_domain_and_honors_robots():
    rules = parse_robots("User-agent: *\nDisallow: /private\nUser-agent: other\nDisallow: /about\n")
    assert path_allowed("/about", rules) is True
    assert path_allowed("/private/file", rules) is False
    html = """
    <a href="/about">About</a>
    <a href="https://news.bbc.co.uk/story">Story</a>
    <a href="https://example.com/offsite">Off</a>
    <a href="mailto:desk@bbc.co.uk">Mail</a>
    """
    links = extract_links("https://www.bbc.co.uk/", html, "bbc.co.uk")
    assert "https://www.bbc.co.uk/about" in links
    assert "https://news.bbc.co.uk/story" in links
    assert all("example.com" not in link for link in links)


def test_crawl_does_not_follow_a_redirect_off_the_registrable_domain(monkeypatch):
    requested: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append((request.url.host, request.url.path))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\n")
        if request.url.path == "/next":
            return httpx.Response(302, headers={"location": "https://news.bbc.co.uk/story"})
        if request.url.host == "news.bbc.co.uk":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><title>Story</title></html>")
        if request.url.path == "/leave":
            return httpx.Response(302, headers={"location": "https://account.bbc.com/signin"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text='<html><title>Home</title><a href="/next">Next</a><a href="/leave">Leave</a></html>',
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = transport
        kwargs.setdefault("trust_env", False)
        return real_client(**kwargs)

    async def allow(_host):
        return None

    monkeypatch.setattr("app.checks.crawl.open_client", client_factory)
    monkeypatch.setattr("app.checks.crawl.pin_host", allow)
    result = asyncio.run(crawl_monitor("https://www.bbc.co.uk/", "bbc.co.uk", None, time.monotonic() + 30))
    assert all(host != "account.bbc.com" for host, _path in requested)
    assert ("news.bbc.co.uk", "/story") in requested
    story = next(page for page in result.pages if page.url.endswith("/next"))
    assert story.status_code == 200
    assert story.title == "Story"
    left = next(page for page in result.pages if page.url.endswith("/leave"))
    assert left.error == OFF_SITE
    assert left.title is None


def test_screenshot_navigation_refuses_private_and_metadata_addresses():
    async def check():
        cache: dict[str, str | None] = {}
        assert await blocked_navigation("http://127.0.0.1/", cache) == "Private address"
        assert await blocked_navigation("http://127.0.0.1/again", cache) == "Private address"
        assert await blocked_navigation("http://169.254.169.254/latest/meta-data", cache) == "Private address"
        assert await blocked_navigation("about:blank", cache) is None

    asyncio.run(check())
