import platform
from datetime import timedelta

from fastapi.testclient import TestClient

from app.capabilities import capabilities
from app.checks.agents import DESKTOP_UA, MOBILE_UA
from app.clock import utcnow
from app.config import Settings
from app import environment
from app.environment import (
    PRIVATE_ADDRESS,
    TraceResponse,
    TraceTarget,
    environment_facts,
    hooks,
    reset_network,
)
from app.main import create_app
from app.notices import notices


def setup_function():
    capabilities.registration = False
    capabilities.browser = False
    capabilities.registration_reason = ""
    capabilities.browser_reason = ""
    notices.reset()
    reset_network()

    async def refuse(url):
        raise AssertionError(f"unexpected trace fetch: {url}")

    hooks.fetch = refuse


def _app(tmp_path, **kwargs):
    settings = Settings(
        data_dir=tmp_path,
        password=kwargs.get("password"),
        secret_key=kwargs.get("secret_key", "test-secret"),
        start_scheduler=False,
        https_only=kwargs.get("https_only", False),
    )
    return create_app(settings)


def test_about_lists_the_process_and_the_scrape_settings(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/about")
        home = client.get("/")
    assert page.status_code == 200
    assert platform.system() in page.text
    assert platform.python_version() in page.text
    assert "127.0.0.1:8000" in page.text
    assert DESKTOP_UA in page.text
    assert MOBILE_UA in page.text
    assert "HTTPS, then HTTP" in page.text
    assert "15 seconds" in page.text
    assert "30 seconds" in page.text
    assert "180 seconds" in page.text
    assert 'href="/about" aria-current="page"' in page.text
    assert "every 30s" in page.text
    assert "every 15s" in home.text
    assert "Checking how this machine reaches the internet." in page.text


def test_about_hides_the_password_and_the_session_secret(tmp_path):
    async def fetch(_url):
        raise ConnectionError("down")

    hooks.fetch = fetch
    settings = Settings(
        data_dir=tmp_path,
        password="s3cret-password",
        secret_key="signing-key-value",
        start_scheduler=False,
        https_only=True,
    )
    facts = environment_facts(settings)
    assert facts.password_set == "Set"
    assert facts.https_only == "On"
    assert "s3cret-password" not in repr(facts)
    assert "signing-key-value" not in repr(facts)
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver") as client:
        blocked = client.get("/about", follow_redirects=False)
        assert blocked.status_code == 303
        assert blocked.headers["location"].startswith("/login?next=")
        fragment = client.get("/fragment/about-network", follow_redirects=False)
        assert fragment.status_code == 303
        assert fragment.headers["location"].startswith("/login")
        token = client.get("/login").text
        import re

        csrf = re.search(r'name="csrf" value="([^"]+)"', token).group(1)
        signed_in = client.post(
            "/login",
            data={"csrf": csrf, "password": "s3cret-password"},
            follow_redirects=False,
        )
        assert signed_in.status_code == 303
        page = client.get("/about")
        network = client.get("/fragment/about-network")
    assert page.status_code == 200
    assert "s3cret-password" not in page.text
    assert "signing-key-value" not in page.text
    assert "<td>Set</td>" in page.text
    assert "<td>On</td>" in page.text
    assert "s3cret-password" not in network.text
    assert "signing-key-value" not in network.text


def test_failed_probe_keeps_the_local_tables_and_shows_the_error(tmp_path):
    async def fetch(_url):
        raise ConnectionError("down")

    hooks.fetch = fetch
    app = _app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/about")
        network = client.get("/fragment/about-network")
    assert page.status_code == 200
    assert platform.system() in page.text
    assert DESKTOP_UA in page.text
    assert MOBILE_UA in page.text
    assert network.status_code == 200
    assert "Connection failed" in network.text
    assert ">No<" in network.text
    assert "down" not in network.text


def test_private_trace_address_is_discarded_and_not_sent_to_rdap(tmp_path):
    seen = []

    async def fetch(_url):
        return TraceResponse(200, "ip=10.1.1.1\nhttp=http/2\ntls=TLSv1.3\n", False)

    def org(address):
        seen.append(address)
        return "Hidden ISP"

    hooks.fetch = fetch
    hooks.org_lookup = org
    hooks.local_route = lambda _address: "192.168.1.9"
    capabilities.registration = True
    app = _app(tmp_path)
    with TestClient(app) as client:
        network = client.get("/fragment/about-network")
    assert network.status_code == 200
    assert "10.1.1.1" not in network.text
    assert "Hidden ISP" not in network.text
    assert "http/2" not in network.text
    assert PRIVATE_ADDRESS in network.text
    assert "192.168.1.9" in network.text
    assert seen == []


def test_public_trace_shows_the_address_and_the_isp(tmp_path):
    seen = []

    async def fetch(url):
        if url.startswith("https://["):
            raise ConnectionError("no v6")
        return TraceResponse(200, "ip=8.8.8.8\nhttp=http/2\ntls=TLSv1.3\n", False)

    def org(address):
        seen.append(address)
        return "Example ISP"

    def reverse(_address):
        return "dns.google"

    hooks.fetch = fetch
    hooks.org_lookup = org
    hooks.reverse_lookup = reverse
    hooks.local_route = lambda _address: "192.168.1.9"
    capabilities.registration = True
    app = _app(tmp_path)
    with TestClient(app) as client:
        network = client.get("/fragment/about-network")
        assert seen == ["8.8.8.8"]
        again = client.get("/fragment/about-network")
        assert seen == ["8.8.8.8"]
        fresh = client.get("/fragment/about-network?fresh=1")
    assert "8.8.8.8" in network.text
    assert "Example ISP" in network.text
    assert "dns.google" in network.text
    assert "http/2" in network.text
    assert "TLSv1.3" in network.text
    assert "Connection failed" in network.text
    assert "Check again" in network.text
    assert "fresh=1" in network.text
    assert again.text == network.text
    assert seen == ["8.8.8.8", "8.8.8.8"]
    assert "8.8.8.8" in fresh.text


def test_registration_off_keeps_the_address_and_skips_the_isp_lookup(tmp_path):
    seen = []

    async def fetch(_url):
        return TraceResponse(200, "ip=8.8.8.8\nhttp=http/2\ntls=TLSv1.3\n", False)

    def org(address):
        seen.append(address)
        return "Example ISP"

    hooks.fetch = fetch
    hooks.org_lookup = org
    capabilities.registration = False
    app = _app(tmp_path)
    with TestClient(app) as client:
        network = client.get("/fragment/about-network")
    assert "8.8.8.8" in network.text
    assert "Registration lookups are unavailable." in network.text
    assert "Example ISP" not in network.text
    assert seen == []


def test_redirect_is_not_followed(tmp_path):
    seen = []

    async def fetch(_url):
        return TraceResponse(302, "ip=8.8.8.8\n", True)

    def org(address):
        seen.append(address)
        return "Example ISP"

    hooks.fetch = fetch
    hooks.org_lookup = org
    capabilities.registration = True
    app = _app(tmp_path)
    with TestClient(app) as client:
        network = client.get("/fragment/about-network")
    assert "Redirect refused" in network.text
    assert "8.8.8.8" not in network.text
    assert seen == []


def test_blocked_target_is_not_contacted(tmp_path, monkeypatch):
    called = []

    async def fetch(url):
        called.append(url)
        return TraceResponse(200, "ip=8.8.8.8\n", False)

    monkeypatch.setattr(
        "app.environment.TRACE_TARGETS",
        (TraceTarget("IPv4", "127.0.0.1", "https://127.0.0.1/cdn-cgi/trace"),),
    )
    hooks.fetch = fetch
    hooks.local_route = lambda address: called.append(address) or "192.168.1.9"
    app = _app(tmp_path)
    with TestClient(app) as client:
        network = client.get("/fragment/about-network")
    assert called == []
    assert "127.0.0.1" not in network.text
    assert PRIVATE_ADDRESS in network.text


def test_stale_snapshot_is_probed_again(tmp_path):
    calls = {"n": 0}

    async def fetch(_url):
        calls["n"] += 1
        return TraceResponse(200, "ip=8.8.8.8\nhttp=http/2\ntls=TLSv1.3\n", False)

    hooks.fetch = fetch
    app = _app(tmp_path)
    with TestClient(app) as client:
        client.get("/fragment/about-network")
        environment._snapshot.probed_at = utcnow() - timedelta(minutes=6)
        client.get("/fragment/about-network")
    assert calls["n"] == 4
