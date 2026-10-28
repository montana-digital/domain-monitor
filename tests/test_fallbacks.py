import asyncio
import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.checks.http_check import HttpResult, probe_hostname
from app.checks.registration import RegistrationRecord, registration_view
from app.checks.runner import build_vantages, execute_check
from app.checks.types import UNEXPECTED, ProxySpec
from app.clock import utcnow
from app.config import Settings
from app import db
from app.db import init_db
from app.main import create_app
from app.models import CheckAttempt, CheckEvent, Monitor, Proxy, RegistrationSnapshot
from app.notices import notices
from app.scheduler import tick
from app.service import queue_ids, refresh_registration, reset_shutdown, run_monitor, shutdown_checks


def _setup(tmp_path):
    settings = Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False)
    init_db(settings)
    notices.reset()
    notices.scheduler_enabled = False
    reset_shutdown()
    return settings


def _monitor(hostname="example.com", **flags) -> int:
    now = utcnow()
    monitor = Monitor(
        hostname=hostname,
        registrable_domain=flags.pop("registrable_domain", "example.com"),
        interval_minutes=5,
        screenshot_enabled=flags.get("screenshot_enabled", False),
        crawl_enabled=flags.get("crawl_enabled", False),
        use_proxy=flags.get("use_proxy", False),
        mobile_ua=flags.get("mobile_ua", False),
        created_at=now,
        updated_at=now,
        next_check_at=now,
        last_partial=False,
    )
    with db.SessionLocal() as session:
        session.add(monitor)
        session.commit()
        session.refresh(monitor)
        return monitor.id


async def _no_sleep(_seconds):
    return None


def test_https_failure_falls_back_to_http():
    calls = []

    async def attempt(scheme):
        calls.append(scheme)
        if scheme == "https":
            return HttpResult(error="Connection failed", scheme=scheme)
        return HttpResult(status_code=200, scheme=scheme, final_url="http://example.com/")

    result = asyncio.run(probe_hostname("example.com", attempt=attempt, sleep=_no_sleep))
    assert result.status_code == 200
    assert calls == ["https", "https", "http"]


def test_503_is_retried_once_on_the_same_scheme():
    calls = []

    async def attempt(scheme):
        calls.append(scheme)
        if len(calls) == 1:
            return HttpResult(status_code=503, scheme=scheme, final_url=f"{scheme}://example.com/")
        return HttpResult(status_code=200, scheme=scheme, final_url=f"{scheme}://example.com/")

    result = asyncio.run(probe_hostname("example.com", attempt=attempt, sleep=_no_sleep))
    assert result.status_code == 200
    assert calls == ["https", "https"]


def test_http_404_does_not_try_the_other_scheme():
    calls = []

    async def attempt(scheme):
        calls.append(scheme)
        return HttpResult(status_code=404, scheme=scheme, final_url=f"{scheme}://example.com/")

    result = asyncio.run(probe_hostname("example.com", attempt=attempt, sleep=_no_sleep))
    assert result.status_code == 404
    assert calls == ["https"]


def test_direct_failure_and_proxy_success_is_saved(tmp_path):
    _setup(tmp_path)
    monitor_id = _monitor(use_proxy=True)
    with db.SessionLocal() as session:
        session.add(Proxy(url="http://proxy.example:8080", enabled=True, created_at=utcnow()))
        session.commit()

    async def probe(hostname, profile, proxy_url):
        if proxy_url is None:
            return HttpResult(error="Timed out")
        return HttpResult(status_code=200, scheme="https", final_url=f"https://{hostname}/", latency_ms=12)

    asyncio.run(run_monitor(monitor_id, "schedule", probe=probe))
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        attempts = session.scalars(select(CheckAttempt).join(CheckEvent).where(CheckEvent.monitor_id == monitor_id)).all()
        assert monitor.last_status_code == 200
        assert monitor.last_error is None
        assert len(attempts) == 2
        assert attempts[0].error == "Timed out"
        assert attempts[1].status_code == 200


def test_screenshot_exception_still_saves_http_status(tmp_path):
    _setup(tmp_path)
    monitor_id = _monitor("shot.example.com", screenshot_enabled=True, registrable_domain="example.com")

    async def probe(hostname, profile, proxy_url):
        return HttpResult(status_code=200, scheme="https", final_url=f"https://{hostname}/", latency_ms=5)

    async def capture(url, profiles, proxy_url):
        raise RuntimeError("browser crashed")

    asyncio.run(run_monitor(monitor_id, "manual", probe=probe, capture=capture))
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        event = session.scalars(select(CheckEvent).where(CheckEvent.monitor_id == monitor_id)).one()
        assert monitor.last_status_code == 200
        assert monitor.last_partial is True
        assert "Screenshot failed" in (monitor.last_partial_reason or "")
        assert event.screenshot_outcome == "failed"
        assert event.screenshot_error == UNEXPECTED


def test_one_monitor_exception_does_not_stop_the_next(tmp_path):
    _setup(tmp_path)
    broken_id = _monitor("broken.example.com", registrable_domain="example.com")
    healthy_id = _monitor("healthy.example.com", registrable_domain="example.com")

    async def probe(hostname, profile, proxy_url):
        if hostname.startswith("broken"):
            raise RuntimeError("boom")
        return HttpResult(status_code=200, scheme="https", final_url=f"https://{hostname}/", latency_ms=4)

    asyncio.run(run_monitor(broken_id, "schedule", probe=probe))
    asyncio.run(run_monitor(healthy_id, "schedule", probe=probe))
    with db.SessionLocal() as session:
        broken = session.scalars(select(CheckEvent).where(CheckEvent.monitor_id == broken_id)).one()
        healthy = session.get(Monitor, healthy_id)
        assert broken.error == UNEXPECTED or broken.attempts
        assert healthy.last_status_code == 200


def test_queue_continues_after_an_enqueue_error():
    seen = []

    def enqueue(monitor_id, trigger):
        seen.append((monitor_id, trigger))
        if monitor_id == 1:
            raise RuntimeError("queue failed")

    queue_ids([1, 2], enqueue)
    assert seen == [(1, "schedule"), (2, "schedule")]


def test_registration_failure_keeps_the_previous_snapshot(tmp_path):
    _setup(tmp_path)
    monitor_id = _monitor()

    def success(_domain):
        return RegistrationRecord(
            True,
            source="rdap",
            registrar="Example Registrar",
            nameservers=["ns.example"],
            statuses=["active"],
        )

    def failure(_domain):
        return RegistrationRecord(False, error="RDAP failed: down. WHOIS failed: down.")

    asyncio.run(refresh_registration(monitor_id, lookup=success))
    asyncio.run(refresh_registration(monitor_id, lookup=failure))
    with db.SessionLocal() as session:
        rows = session.scalars(
            select(RegistrationSnapshot)
            .where(RegistrationSnapshot.monitor_id == monitor_id)
            .order_by(RegistrationSnapshot.fetched_at.desc())
        ).all()
        view = registration_view(list(rows))
        assert len(rows) == 2
        assert view.failed is True
        assert view.record.registrar == "Example Registrar"
        assert "RDAP failed" in view.error


def test_vantage_list_is_capped():
    proxies = [ProxySpec(index, f"p{index}", f"http://10.0.0.{index}:8080") for index in range(1, 6)]
    assert len(build_vantages(True, True, proxies)) == 6


def test_deadline_cancels_a_slow_probe_and_skips_later_work():
    async def probe(hostname, profile, proxy_url):
        await asyncio.sleep(2)
        return HttpResult(status_code=200, scheme="https", final_url=f"https://{hostname}/")

    async def capture(url, profiles, proxy_url):
        raise AssertionError("screenshot should not start")

    outcome = asyncio.run(
        execute_check(
            hostname="example.com",
            registrable_domain="example.com",
            screenshot=True,
            crawl=True,
            use_proxy=False,
            mobile=False,
            proxies=[],
            probe=probe,
            capture=capture,
            crawl_site=_unused_crawl,
            deadline_seconds=0.05,
        )
    )
    assert outcome.attempts[0].error == "Check timed out"
    assert outcome.status_code is None
    assert outcome.screenshot_error == "Check timed out"
    assert outcome.crawl_error == "Check timed out"


def test_shutdown_waits_then_cancels_inflight_checks():
    from app import service

    async def scenario():
        reset_shutdown()
        release = asyncio.Event()

        async def slow():
            await release.wait()

        task = asyncio.create_task(slow())
        service._tasks.add(task)
        task.add_done_callback(service._tasks.discard)
        await shutdown_checks(timeout=0.05)
        assert task.cancelled()
        assert service.try_enqueue(1, "manual") is False
        reset_shutdown()

    asyncio.run(scenario())


def test_tick_stamps_the_scheduler_after_the_work(monkeypatch):
    notices.reset()
    notices.last_tick = None

    def due():
        assert notices.last_tick is None
        return []

    monkeypatch.setattr("app.scheduler.due_monitor_ids", due)
    monkeypatch.setattr("app.scheduler.registration_due_ids", lambda: [])
    monkeypatch.setattr("app.scheduler.maybe_retain", lambda: None)
    asyncio.run(tick())
    assert notices.last_tick is not None


def test_execute_check_reports_partial_without_a_database():
    async def probe(hostname, profile, proxy_url):
        return HttpResult(status_code=200, scheme="https", final_url=f"https://{hostname}/")

    async def capture(url, profiles, proxy_url):
        raise RuntimeError("down")

    outcome = asyncio.run(
        execute_check(
            hostname="example.com",
            registrable_domain="example.com",
            screenshot=True,
            crawl=False,
            use_proxy=False,
            mobile=False,
            proxies=[],
            probe=probe,
            capture=capture,
            crawl_site=_unused_crawl,
        )
    )
    assert outcome.status_code == 200
    assert outcome.partial is True


async def _unused_crawl(*_args, **_kwargs):
    raise AssertionError("crawl should not run")


def test_home_and_rejected_domain(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "Domain Monitor" in home.text
        assert "No domains yet" in home.text
        form = client.get("/domains/new")
        token = re.search(r'name="csrf" value="([^"]+)"', form.text).group(1)
        rejected = client.post(
            "/domains",
            data={"csrf": token, "domain": "google.notavalidsuffix", "interval_minutes": "5"},
        )
        assert rejected.status_code == 400
        assert "Unknown public suffix" in rejected.text
        missing = client.get("/domains/9999")
        assert missing.status_code == 404


def test_theme_chrome(tmp_path):
    root = Path(__file__).resolve().parents[1]
    css = (root / "app" / "static" / "style.css").read_text(encoding="utf-8")
    assert "#0b0b0c" in css
    assert "#fbfaf7" in css
    fonts = root / "app" / "static" / "fonts"
    for name in (
        "inter-latin-wght-normal.woff2",
        "newsreader-latin-wght-normal.woff2",
        "jetbrains-mono-latin-wght-normal.woff2",
    ):
        assert f"/static/fonts/{name}" in css
        assert (fonts / name).is_file()

    app = create_app(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "~/domain-monitor" in home.text
        assert 'data-theme-choice="dark"' in home.text
        assert 'data-theme-choice="light"' in home.text
        assert 'localStorage.getItem("tn-theme")' in home.text
        assert 'class="site-footer"' in home.text

    login_app = create_app(
        Settings(data_dir=tmp_path / "login", password="secret", secret_key="test-secret", start_scheduler=False)
    )
    with TestClient(login_app) as client:
        login = client.get("/login")
        assert login.status_code == 200
        assert "theme-choice" in login.text
        assert 'aria-label="Theme"' in login.text
        assert 'aria-label="Primary"' not in login.text
