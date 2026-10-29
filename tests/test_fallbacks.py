import asyncio
import re
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.checks.http_check import HttpProber, HttpResult, probe_hostname
from app.checks.registration import RegistrationRecord, registrar_from_rdap, registration_view
from app.checks.runner import build_vantages, execute_check
from app.checks.types import UNEXPECTED, ProxySpec
from app.clock import as_utc, utcnow
from app.config import Settings
from app import db
from app.db import init_db
from app.main import create_app
from app.models import CheckAttempt, CheckEvent, Monitor, Proxy, RegistrationSnapshot
from app.notices import notices
from app.scheduler import tick
from app.service import (
    queue_ids,
    refresh_registration,
    registration_due_ids,
    reset_shutdown,
    run_monitor,
    save_snapshot,
    shutdown_checks,
    step_attention,
    try_enqueue,
    try_enqueue_registration,
)


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


def test_hung_registration_frees_its_slot(tmp_path, monkeypatch):
    import threading
    import time

    from app import service

    _setup(tmp_path)
    monitor_id = _monitor()
    release = threading.Event()
    monkeypatch.setattr(service, "REGISTRATION_TIMEOUT", 0.05)

    def hang(_domain):
        release.wait(timeout=5)
        return RegistrationRecord(True, source="rdap", registrar="Too late")

    async def scenario():
        started = time.monotonic()
        await refresh_registration(monitor_id, lookup=hang)
        assert time.monotonic() - started < 2
        assert monitor_id not in service._reg_inflight
        sem = service._semaphore("registration", 2)
        await asyncio.wait_for(sem.acquire(), 0.2)
        await asyncio.wait_for(sem.acquire(), 0.2)
        sem.release()
        sem.release()
        release.set()

    asyncio.run(scenario())
    with db.SessionLocal() as session:
        row = session.scalars(
            select(RegistrationSnapshot).where(RegistrationSnapshot.monitor_id == monitor_id)
        ).one()
        assert row.success is False
        assert row.error == "Registration lookup timed out."


def test_shutdown_cancels_a_registration_task(tmp_path, monkeypatch):
    import threading

    from app import service

    _setup(tmp_path)
    monitor_id = _monitor("reg.example.com")
    entered = threading.Event()
    release = threading.Event()

    def hang(_domain):
        entered.set()
        release.wait(timeout=5)
        return RegistrationRecord(False, "late")

    monkeypatch.setattr(service, "registration_available", lambda: True)
    monkeypatch.setattr(service, "_real_lookup", hang)
    monkeypatch.setattr(service, "REGISTRATION_TIMEOUT", 30)

    async def scenario():
        reset_shutdown()
        assert try_enqueue_registration(monitor_id) is True
        assert await asyncio.to_thread(entered.wait, 2)
        await shutdown_checks(timeout=0.05)
        assert monitor_id not in service._reg_inflight
        assert all(task.done() for task in service._tasks)
        release.set()
        reset_shutdown()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        reset_shutdown()


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


def test_registration_due_ids_is_one_grouped_lookup(tmp_path):
    _setup(tmp_path)
    fresh_id = _monitor("fresh.example.com")
    stale_id = _monitor("stale.example.com")
    missing_id = _monitor("missing.example.com")
    save_snapshot(fresh_id, RegistrationRecord(True, source="rdap", registrar="Fresh"))
    save_snapshot(stale_id, RegistrationRecord(True, source="rdap", registrar="Stale"))
    with db.SessionLocal() as session:
        stale = session.scalars(
            select(RegistrationSnapshot).where(RegistrationSnapshot.monitor_id == stale_id)
        ).one()
        stale.fetched_at = utcnow() - timedelta(hours=25)
        session.commit()
    due = set(registration_due_ids())
    assert stale_id in due
    assert missing_id in due
    assert fresh_id not in due


def test_tick_runs_database_work_off_the_event_loop(monkeypatch):
    import threading

    seen = {}

    def due():
        seen["due"] = threading.get_ident()
        return []

    def lookups():
        seen["lookups"] = threading.get_ident()
        return []

    def retain():
        seen["retain"] = threading.get_ident()

    def step():
        seen["step"] = threading.get_ident()

    monkeypatch.setattr("app.scheduler.registration_available", lambda: True)
    monkeypatch.setattr("app.scheduler.step_attention", step)
    monkeypatch.setattr("app.scheduler.due_monitor_ids", due)
    monkeypatch.setattr("app.scheduler.registration_due_ids", lookups)
    monkeypatch.setattr("app.scheduler.maybe_retain", retain)

    async def scenario():
        seen["loop"] = threading.get_ident()
        await tick()

    asyncio.run(scenario())
    assert seen["step"] != seen["loop"]
    assert seen["due"] != seen["loop"]
    assert seen["lookups"] != seen["loop"]
    assert seen["retain"] != seen["loop"]


def test_schedule_caps_check_tasks_at_eight(tmp_path, monkeypatch):
    from app import service

    _setup(tmp_path)
    ids = [_monitor(f"cap{index}.example.com") for index in range(20)]

    async def probe(self, hostname, profile, proxy_url):
        await asyncio.sleep(30)
        return HttpResult(status_code=200, scheme="https", final_url=f"https://{hostname}/")

    monkeypatch.setattr(HttpProber, "probe", probe)

    async def scenario():
        reset_shutdown()
        queued = [try_enqueue(monitor_id, "schedule") for monitor_id in ids]
        assert queued.count(True) == 8
        assert len(service._inflight) == 8
        await shutdown_checks(timeout=0.05)
        reset_shutdown()
        assert service._inflight == set()
        assert service._occupying == set()
        again = [try_enqueue(monitor_id, "schedule") for monitor_id in ids]
        assert again.count(True) == 8
        await shutdown_checks(timeout=0.05)
        reset_shutdown()

    asyncio.run(scenario())


def test_browser_wait_does_not_consume_the_check_slots(tmp_path, monkeypatch):
    from app import service
    from app.checks.types import Shot

    _setup(tmp_path)
    monkeypatch.setattr(service, "browser_available", lambda: True)
    waiting_ids = [_monitor(f"shot{index}.example.com", screenshot_enabled=True) for index in range(8)]
    probe_id = _monitor("open.example.com")
    in_capture = asyncio.Event()
    release_capture = asyncio.Event()
    probe_ran = asyncio.Event()
    entered = 0

    async def capture(url, profiles, proxy_url):
        nonlocal entered
        entered += 1
        if entered >= 8:
            in_capture.set()
        await release_capture.wait()
        return [Shot("desktop", error="stopped")]

    async def probe(self, hostname, profile, proxy_url):
        if hostname.startswith("open."):
            probe_ran.set()
        return HttpResult(status_code=200, scheme="https", final_url=f"https://{hostname}/")

    monkeypatch.setattr(service, "capture_views", capture)
    monkeypatch.setattr(HttpProber, "probe", probe)

    async def scenario():
        reset_shutdown()
        for monitor_id in waiting_ids:
            assert try_enqueue(monitor_id, "schedule") is True
        await asyncio.wait_for(in_capture.wait(), 2)
        assert service._occupying == set()
        assert try_enqueue(probe_id, "schedule") is True
        await asyncio.wait_for(probe_ran.wait(), 2)
        release_capture.set()
        pending = {task for task in service._tasks if not task.done()}
        if pending:
            await asyncio.wait(pending, timeout=2)
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


def test_busy_save_records_the_measured_status(tmp_path, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app import service
    from app.checks.types import Shot
    from app.db import commit_with_retry
    from app.models import Screenshot

    _setup(tmp_path)
    monitor_id = _monitor("busy.example.com", screenshot_enabled=True)
    calls = {"n": 0}

    def flaky(session):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return commit_with_retry(session)

    monkeypatch.setattr(service, "commit_with_retry", flaky)
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)

    async def probe(hostname, profile, proxy_url):
        return HttpResult(status_code=200, scheme="https", final_url=f"https://{hostname}/", latency_ms=4)

    async def capture(url, profiles, proxy_url):
        return [Shot("desktop", data=b"\xff\xd8\xff")]

    asyncio.run(run_monitor(monitor_id, "schedule", probe=probe, capture=capture))
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        event = session.scalars(select(CheckEvent).where(CheckEvent.monitor_id == monitor_id)).one()
        attempts = session.scalars(select(CheckAttempt).where(CheckAttempt.check_event_id == event.id)).all()
        shots = session.scalars(select(Screenshot).where(Screenshot.check_event_id == event.id)).all()
        assert event.status_code == 200
        assert event.error == "Result could not be stored."
        assert attempts == []
        assert shots == []
        assert monitor.last_status_code == 200
        assert as_utc(monitor.next_check_at) - as_utc(monitor.last_checked_at) == timedelta(minutes=5)
    assert list((tmp_path / "screenshots").glob("*.jpg")) == []


def test_login_accepts_the_password_and_rejects_a_wrong_one(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, password="secret", secret_key="test-secret", start_scheduler=False))
    with TestClient(app) as client:
        blocked = client.get("/", follow_redirects=False)
        assert blocked.status_code == 303
        assert blocked.headers["location"].startswith("/login")
        token = _csrf(client, "/login")
        wrong = client.post("/login", data={"csrf": token, "password": "nope"}, follow_redirects=False)
        assert wrong.status_code == 401
        assert "Incorrect password." in wrong.text
        token = re.search(r'name="csrf" value="([^"]+)"', wrong.text).group(1)
        ok = client.post("/login", data={"csrf": token, "password": "secret"}, follow_redirects=False)
        assert ok.status_code == 303
        assert ok.headers["location"] == "/"
        home = client.get("/")
        assert home.status_code == 200
        assert "Domain Monitor" in home.text


def test_login_is_throttled_after_repeated_failures(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, password="secret", secret_key="test-secret", start_scheduler=False))
    with TestClient(app) as client:
        last = None
        for _ in range(5):
            token = _csrf(client, "/login")
            last = client.post("/login", data={"csrf": token, "password": "nope"}, follow_redirects=False)
            assert last.status_code == 401
        token = re.search(r'name="csrf" value="([^"]+)"', last.text).group(1)
        blocked = client.post("/login", data={"csrf": token, "password": "secret"}, follow_redirects=False)
        assert blocked.status_code == 429
        assert "Too many login attempts" in blocked.text


def test_https_only_cookie_is_off_unless_requested(tmp_path, monkeypatch):
    monkeypatch.delenv("DOMAIN_MONITOR_HTTPS_ONLY", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "env"))
    monkeypatch.setenv("DOMAIN_MONITOR_SECRET", "env-secret")
    assert Settings.from_env().https_only is False
    monkeypatch.setenv("DOMAIN_MONITOR_HTTPS_ONLY", "yes")
    assert Settings.from_env().https_only is True

    plain = create_app(Settings(data_dir=tmp_path / "plain", password="secret", secret_key="test-secret", start_scheduler=False))
    with TestClient(plain) as client:
        token = _csrf(client, "/login")
        response = client.post("/login", data={"csrf": token, "password": "secret"}, follow_redirects=False)
        assert "secure" not in response.headers.get("set-cookie", "").lower()

    secure = create_app(
        Settings(
            data_dir=tmp_path / "secure",
            password="secret",
            secret_key="test-secret",
            start_scheduler=False,
            https_only=True,
        )
    )
    with TestClient(secure) as client:
        token = _csrf(client, "/login")
        response = client.post("/login", data={"csrf": token, "password": "secret"}, follow_redirects=False)
        assert "secure" in response.headers.get("set-cookie", "").lower()


def test_missing_csrf_token_is_rejected(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, password="secret", secret_key="test-secret", start_scheduler=False))
    with TestClient(app) as client:
        client.get("/login")
        rejected = client.post("/login", data={"password": "secret"}, follow_redirects=False)
        assert rejected.status_code == 403
        home = client.get("/", follow_redirects=False)
        assert home.status_code == 303


def test_home_and_rejected_domain(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "Domain Monitor" in home.text
        assert "No watched domains" in home.text
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


def _csrf(client, path: str) -> str:
    page = client.get(path)
    assert page.status_code == 200
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def _saved_monitor(hostname: str, *, interval: int, checked, due, attention: str = "watch") -> int:
    now = utcnow()
    monitor = Monitor(
        hostname=hostname,
        registrable_domain="example.com",
        interval_minutes=interval,
        attention=attention,
        watch_days=30,
        screenshot_enabled=False,
        crawl_enabled=False,
        use_proxy=False,
        mobile_ua=False,
        created_at=now,
        updated_at=now,
        last_checked_at=checked,
        next_check_at=due,
        last_partial=False,
    )
    with db.SessionLocal() as session:
        session.add(monitor)
        session.commit()
        session.refresh(monitor)
        return monitor.id


def test_marking_watch_pulls_the_next_check_forward(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    checked = utcnow() - timedelta(minutes=30)
    due = checked + timedelta(days=7)
    with TestClient(app) as client:
        monitor_id = _saved_monitor(
            "soon.example.com", interval=10080, checked=checked, due=due, attention="routine"
        )
        token = _csrf(client, f"/domains/{monitor_id}")
        saved = client.post(
            f"/domains/{monitor_id}/edit",
            data={"csrf": token, "watch": "yes", "watch_days": "30"},
            follow_redirects=False,
        )
        assert saved.status_code == 303
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        assert monitor.attention == "watch"
        assert monitor.interval_minutes == 30
        assert as_utc(monitor.next_check_at) == as_utc(monitor.last_checked_at) + timedelta(minutes=30)
        assert as_utc(monitor.next_check_at) < as_utc(due)


def test_leaving_watch_waits_for_the_slower_interval(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    checked = utcnow() - timedelta(minutes=1)
    due = utcnow() + timedelta(minutes=20)
    with TestClient(app) as client:
        monitor_id = _saved_monitor("later.example.com", interval=30, checked=checked, due=due, attention="watch")
        token = _csrf(client, f"/domains/{monitor_id}")
        saved = client.post(
            f"/domains/{monitor_id}/edit",
            data={"csrf": token, "watch_days": "30"},
            follow_redirects=False,
        )
        assert saved.status_code == 303
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        assert monitor.attention == "routine"
        assert monitor.interval_minutes == 240
        assert as_utc(monitor.next_check_at) == as_utc(checked) + timedelta(hours=4)


def test_health_succeeds_without_a_session_when_the_scheduler_is_off(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, password="secret", secret_key="test-secret", start_scheduler=False))
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        notices.scheduler_enabled = True
        notices.last_tick = utcnow()
        fresh = client.get("/health")
        assert fresh.status_code == 200
        assert fresh.json() == {"status": "ok"}


def test_health_reports_a_stalled_scheduler(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    with TestClient(app) as client:
        notices.scheduler_enabled = True
        notices.started_at = utcnow() - timedelta(seconds=90)
        notices.last_tick = None
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json() == {"status": "error", "scheduler": "stalled"}


def test_health_reports_a_database_that_cannot_be_opened(tmp_path):
    from sqlalchemy import create_engine

    app = create_app(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    with TestClient(app) as client:
        missing = tmp_path / "absent" / "monitor.db"
        db.engine = create_engine(f"sqlite:///{missing.as_posix()}")
        response = client.get("/health")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "error"
        assert body["database"] == "unavailable"
        assert "scheduler" not in body


def test_registration_banner_keeps_another_monitors_failure(tmp_path):
    _setup(tmp_path)
    failed_id = _monitor("failed.example.com")
    ok_id = _monitor("ok.example.com")
    save_snapshot(failed_id, RegistrationRecord(False, "RDAP failed"))
    save_snapshot(ok_id, RegistrationRecord(True, source="rdap", registrar="Example Registrar"))
    app = create_app(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "Registration lookup failed for failed.example.com: RDAP failed" in home.text
        assert "Registration lookup failed for ok.example.com" not in home.text
        fragment = client.get("/notices")
        assert "Registration lookup failed for failed.example.com: RDAP failed" in fragment.text
        save_snapshot(failed_id, RegistrationRecord(True, source="rdap", registrar="Example Registrar"))
        cleared = client.get("/notices")
        assert "Registration lookup failed" not in cleared.text


def test_expired_watch_becomes_a_four_hour_routine(tmp_path):
    _setup(tmp_path)
    now = utcnow()
    monitor_id = _monitor("expire.example.com")
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        monitor.attention = "watch"
        monitor.interval_minutes = 30
        monitor.watch_until = now - timedelta(minutes=1)
        monitor.last_checked_at = now - timedelta(minutes=10)
        monitor.next_check_at = now
        session.commit()
    step_attention()
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        assert monitor.attention == "routine"
        assert monitor.interval_minutes == 240
        assert as_utc(monitor.next_check_at) == as_utc(monitor.last_checked_at) + timedelta(hours=4)


def test_routine_steps_to_daily_then_weekly(tmp_path):
    _setup(tmp_path)
    now = utcnow()
    daily_id = _monitor("daily.example.com")
    weekly_id = _monitor("weekly.example.com")
    with db.SessionLocal() as session:
        daily = session.get(Monitor, daily_id)
        weekly = session.get(Monitor, weekly_id)
        daily.attention = "routine"
        daily.routine_since = now - timedelta(days=31)
        daily.interval_minutes = 240
        weekly.attention = "routine"
        weekly.routine_since = now - timedelta(days=91)
        weekly.interval_minutes = 240
        session.commit()
    step_attention()
    with db.SessionLocal() as session:
        assert session.get(Monitor, daily_id).interval_minutes == 1440
        assert session.get(Monitor, weekly_id).interval_minutes == 10080


def test_chosen_watch_interval_survives_the_scheduler_and_a_status_change(tmp_path, monkeypatch):
    monkeypatch.setattr("app.routes.pages.try_enqueue", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.routes.pages.try_enqueue_registration", lambda *_args, **_kwargs: False)
    app = create_app(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    with TestClient(app) as client:
        token = _csrf(client, "/domains/new")
        rejected = client.post(
            "/domains",
            data={"csrf": token, "domain": "bad.example.com", "watch": "yes", "interval_minutes": "7"},
        )
        assert rejected.status_code == 400
        assert "Check interval must be one of 5, 15, 30, 60, 240 minutes." in rejected.text
        saved = client.post(
            "/domains",
            data={
                "csrf": token,
                "domain": "five.example.com",
                "watch": "yes",
                "watch_days": "30",
                "interval_minutes": "5",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
    with db.SessionLocal() as session:
        monitor = session.scalar(select(Monitor).where(Monitor.hostname == "five.example.com"))
        assert monitor.attention == "watch"
        assert monitor.interval_minutes == 5
        monitor_id = monitor.id
    step_attention()
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        assert monitor.attention == "watch"
        assert monitor.interval_minutes == 5
        monitor.last_checked_at = utcnow() - timedelta(minutes=10)
        monitor.last_status_code = 200
        session.commit()

    async def probe(hostname, profile, proxy_url):
        return HttpResult(status_code=500, scheme="https", final_url=f"https://{hostname}/", latency_ms=4)

    asyncio.run(run_monitor(monitor_id, "manual", probe=probe))
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        assert monitor.attention == "watch"
        assert monitor.interval_minutes == 5
        assert monitor.last_status_code == 500
        assert as_utc(monitor.watch_until) > utcnow() + timedelta(days=29)


def test_a_slower_watch_interval_waits(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    checked = utcnow() - timedelta(minutes=1)
    due = checked + timedelta(minutes=4)
    with TestClient(app) as client:
        monitor_id = _saved_monitor("slow.example.com", interval=5, checked=checked, due=due, attention="watch")
        token = _csrf(client, f"/domains/{monitor_id}")
        saved = client.post(
            f"/domains/{monitor_id}/edit",
            data={"csrf": token, "watch": "yes", "watch_days": "30", "interval_minutes": "60"},
            follow_redirects=False,
        )
        assert saved.status_code == 303
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        assert monitor.interval_minutes == 60
        assert as_utc(monitor.next_check_at) == as_utc(checked) + timedelta(minutes=60)


def test_a_status_change_extends_the_watch_window(tmp_path):
    _setup(tmp_path)
    now = utcnow()
    monitor_id = _monitor("change.example.com")
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        monitor.attention = "watch"
        monitor.watch_days = 30
        monitor.watch_until = now + timedelta(days=1)
        monitor.interval_minutes = 30
        monitor.last_checked_at = now - timedelta(hours=1)
        monitor.last_status_code = 200
        session.commit()

    async def probe(hostname, profile, proxy_url):
        return HttpResult(status_code=500, scheme="https", final_url=f"https://{hostname}/", latency_ms=4)

    asyncio.run(run_monitor(monitor_id, "manual", probe=probe))
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        assert monitor.attention == "watch"
        assert monitor.last_status_code == 500
        assert as_utc(monitor.watch_until) > now + timedelta(days=29)


def test_a_routine_change_restarts_at_four_hours(tmp_path):
    _setup(tmp_path)
    now = utcnow()
    monitor_id = _monitor("restart.example.com")
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        monitor.attention = "routine"
        monitor.interval_minutes = 10080
        monitor.routine_since = now - timedelta(days=100)
        monitor.last_checked_at = now - timedelta(days=1)
        monitor.last_status_code = 200
        session.commit()

    async def probe(hostname, profile, proxy_url):
        return HttpResult(status_code=404, scheme="https", final_url=f"https://{hostname}/", latency_ms=4)

    asyncio.run(run_monitor(monitor_id, "manual", probe=probe))
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        assert monitor.attention == "routine"
        assert monitor.interval_minutes == 240
        assert as_utc(monitor.routine_since) > now - timedelta(minutes=1)


def test_not_found_lookup_is_stored_as_unregistered(tmp_path):
    _setup(tmp_path)
    monitor_id = _monitor("missing.example.net")

    def lookup(_domain):
        return RegistrationRecord(True, registered=False, source="rdap")

    asyncio.run(refresh_registration(monitor_id, lookup=lookup))
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        snapshot = session.scalars(select(RegistrationSnapshot).where(RegistrationSnapshot.monitor_id == monitor_id)).one()
        assert monitor.registered is False
        assert monitor.registrar is None
        assert snapshot.success is True
        assert snapshot.registered is False


def test_registrar_name_comes_from_the_rdap_entity():
    assert registrar_from_rdap({"entities": {"registrar": [{"name": "MarkMonitor Inc."}]}}) == "MarkMonitor Inc."
    assert registrar_from_rdap({}) is None


def test_failed_lookup_keeps_registrar_and_host(tmp_path):
    _setup(tmp_path)
    monitor_id = _monitor("kept.example.com")
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        monitor.last_address = "1.1.1.1"
        session.commit()

    def success(_domain):
        return RegistrationRecord(True, source="rdap", registrar="Example Registrar")

    def host(_address):
        return "Cloudflare, Inc."

    asyncio.run(refresh_registration(monitor_id, lookup=success, host_lookup=host))

    def failure(_domain):
        return RegistrationRecord(False, "RDAP failed: down")

    def boom(_address):
        raise RuntimeError("rdap down")

    asyncio.run(refresh_registration(monitor_id, lookup=failure, host_lookup=boom))
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        assert monitor.registered is True
        assert monitor.registrar == "Example Registrar"
        assert monitor.host_org == "Cloudflare, Inc."


def test_private_address_skips_host_lookup(tmp_path):
    _setup(tmp_path)
    monitor_id = _monitor("local.example.com")
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        monitor.last_address = "127.0.0.1"
        session.commit()

    def success(_domain):
        return RegistrationRecord(True, source="rdap", registrar="Example Registrar")

    def host(_address):
        raise AssertionError("private addresses are not looked up")

    asyncio.run(refresh_registration(monitor_id, lookup=success, host_lookup=host))
    with db.SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        assert monitor.registrar == "Example Registrar"
        assert monitor.host_org is None


def test_check_saves_a_public_address(tmp_path):
    from app.checks.connect import address_pins

    _setup(tmp_path)
    monitor_id = _monitor("pinned.example.com")

    async def probe(hostname, profile, proxy_url):
        address_pins.set({hostname: ["127.0.0.1", "1.1.1.1"]})
        return HttpResult(status_code=200, scheme="https", final_url=f"https://{hostname}/", latency_ms=3)

    asyncio.run(run_monitor(monitor_id, "schedule", probe=probe))
    with db.SessionLocal() as session:
        assert session.get(Monitor, monitor_id).last_address == "1.1.1.1"


def test_host_org_prefers_the_registrant_name():
    from app.checks.registration import host_org_from_rdap, is_not_registered_message

    data = {"name": "CLOUDFLARENET", "entities": {"registrant": [{"name": "Cloudflare, Inc."}]}}
    assert host_org_from_rdap(data) == "Cloudflare, Inc."
    assert is_not_registered_message("No match for example.com")
    assert not is_not_registered_message("connection timed out")


def test_existing_database_gains_watch_columns(tmp_path):
    import sqlite3

    from app.models import AppSetting

    path = tmp_path / "monitor.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE monitors (
            id INTEGER PRIMARY KEY,
            hostname VARCHAR(253) NOT NULL UNIQUE,
            registrable_domain VARCHAR(253) NOT NULL,
            interval_minutes INTEGER NOT NULL,
            screenshot_enabled BOOLEAN NOT NULL,
            crawl_enabled BOOLEAN NOT NULL,
            use_proxy BOOLEAN NOT NULL,
            mobile_ua BOOLEAN NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            last_checked_at DATETIME,
            next_check_at DATETIME,
            last_status_code INTEGER,
            last_error TEXT,
            last_partial BOOLEAN NOT NULL,
            last_partial_reason VARCHAR(300),
            last_scheme VARCHAR(8)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO monitors (
            hostname, registrable_domain, interval_minutes, screenshot_enabled, crawl_enabled,
            use_proxy, mobile_ua, created_at, updated_at, last_checked_at, last_partial
        ) VALUES ('old.example.com', 'example.com', 5, 0, 0, 0, 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00', '2026-01-02 00:00:00', 0)
        """
    )
    connection.execute("CREATE TABLE app_settings (id INTEGER PRIMARY KEY, retention_days INTEGER NOT NULL)")
    connection.execute("INSERT INTO app_settings (id, retention_days) VALUES (1, 90)")
    connection.commit()
    connection.close()
    init_db(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    with db.SessionLocal() as session:
        monitor = session.scalars(select(Monitor).where(Monitor.hostname == "old.example.com")).one()
        setting = session.get(AppSetting, 1)
        assert monitor.attention == "routine"
        assert monitor.interval_minutes == 240
        assert monitor.quiet_since is not None
        assert setting.schema_version == 1
        assert setting.retention_days == 90
        assert setting.sample_seeded is False


def test_sample_seed_inserts_fifty_domains_once(tmp_path):
    from app.seed import SAMPLE_ROUTINE, SAMPLE_WATCH

    assert len(SAMPLE_WATCH) == 40
    assert len(SAMPLE_ROUTINE) == 10
    settings = Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False, seed_sample=True)
    init_db(settings)
    with db.SessionLocal() as session:
        routine = session.scalars(
            select(Monitor).where(Monitor.attention == "routine").order_by(Monitor.quiet_since.asc())
        ).all()
        assert len(session.scalars(select(Monitor)).all()) == 50
        assert len(routine) == 10
        quiets = [as_utc(row.quiet_since) for row in routine]
        assert quiets == sorted(quiets)
        assert {row.interval_minutes for row in routine} == {240, 1440, 10080}
        doomed = routine[0]
        session.delete(doomed)
        session.commit()
    init_db(settings)
    with db.SessionLocal() as session:
        assert len(session.scalars(select(Monitor)).all()) == 49
