import asyncio
import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import db
from app.capabilities import (
    REGISTRATION_UNREACHABLE,
    browser_status,
    capabilities,
    probe_registration,
)
from app.clock import utcnow
from app.config import Settings
from app.db import init_db
from app.main import create_app
from app.models import CheckEvent, Monitor
from app.notices import notices
from app.routes import pages
from app.scheduler import tick
from app.service import reset_shutdown, run_monitor, try_enqueue_registration
from app.checks.types import HttpResult


def setup_function():
    capabilities.registration = False
    capabilities.browser = False
    capabilities.registration_reason = ""
    capabilities.browser_reason = ""
    notices.reset()


def test_rdap_bootstrap_failure_leaves_registration_off():
    missing, missing_reason = probe_registration(imports_ok=False, timeout=1)
    assert missing is False
    assert "not installed" in missing_reason

    def fail():
        raise RuntimeError("bootstrap refused")

    failed, failed_reason = probe_registration(imports_ok=True, bootstrap=fail, timeout=1)
    assert failed is False
    assert failed_reason == REGISTRATION_UNREACHABLE

    def hang():
        time.sleep(2)

    timed_out, timed_reason = probe_registration(imports_ok=True, bootstrap=hang, timeout=0.05)
    assert timed_out is False
    assert timed_reason == REGISTRATION_UNREACHABLE

    reached, reason = probe_registration(imports_ok=True, bootstrap=lambda: None, timeout=1)
    assert reached is True
    assert reason == ""


def test_missing_chromium_binary_turns_screenshots_off():
    enabled, reason = browser_status(playwright_ok=True, chromium_ok=False)
    assert enabled is False
    assert "Chromium" in reason
    present, present_reason = browser_status(playwright_ok=True, chromium_ok=True)
    assert present is True
    assert present_reason == ""
    absent, absent_reason = browser_status(playwright_ok=False, chromium_ok=True)
    assert absent is False
    assert "Playwright" in absent_reason


def test_missing_browser_skips_capture_and_keeps_the_saved_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("app.service.browser_available", lambda: False)
    settings = Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False)
    init_db(settings)
    reset_shutdown()
    now = utcnow()
    with db.SessionLocal() as session:
        monitor = Monitor(
            hostname="shot.example.com",
            registrable_domain="example.com",
            interval_minutes=5,
            screenshot_enabled=True,
            crawl_enabled=False,
            use_proxy=False,
            mobile_ua=False,
            created_at=now,
            updated_at=now,
            last_partial=False,
        )
        session.add(monitor)
        session.commit()
        monitor_id = monitor.id

    def capture(url, profiles, proxy_url):
        raise AssertionError("capture should not start")

    async def probe(hostname, profile, proxy_url):
        return HttpResult(status_code=200, scheme="https", final_url=f"https://{hostname}/", latency_ms=5)

    monkeypatch.setattr("app.service.capture_views", capture)
    asyncio.run(run_monitor(monitor_id, "schedule", probe=probe))
    with db.SessionLocal() as session:
        saved = session.get(Monitor, monitor_id)
        event = session.scalars(select(CheckEvent).where(CheckEvent.monitor_id == monitor_id)).one()
        assert saved.screenshot_enabled is True
        assert event.screenshot_outcome == "skipped"
        assert event.partial is False


def test_saving_keeps_screenshot_flag_when_the_checkbox_is_omitted(monkeypatch):
    monkeypatch.setattr(pages, "browser_available", lambda: False)
    watching, days, minutes, screenshot, crawl, use_proxy, mobile, error = pages._monitor_fields(
        {"watch": "yes", "watch_days": "30"},
        keep_screenshot=True,
    )
    assert error is None
    assert watching is True
    assert days == 30
    assert minutes == 30
    assert screenshot is True
    assert crawl is False
    assert use_proxy is False
    assert mobile is False

    monkeypatch.setattr(pages, "browser_available", lambda: True)
    _watching, _days, _minutes, cleared, *_rest = pages._monitor_fields(
        {},
        keep_screenshot=True,
    )
    assert cleared is False


def test_scheduler_does_not_list_registration_when_it_is_off(monkeypatch):
    monkeypatch.setattr("app.scheduler.registration_available", lambda: False)
    monkeypatch.setattr("app.scheduler.step_attention", lambda: None)
    monkeypatch.setattr("app.scheduler.due_monitor_ids", lambda: [])
    monkeypatch.setattr("app.scheduler.maybe_retain", lambda: None)

    def lookups():
        raise AssertionError("registration should not be listed")

    monkeypatch.setattr("app.scheduler.registration_due_ids", lookups)
    asyncio.run(tick())


def test_enqueue_registration_returns_false_when_the_feature_is_off():
    capabilities.registration = False

    async def attempt():
        return try_enqueue_registration(1)

    assert asyncio.run(attempt()) is False


def test_detail_page_says_registration_is_unavailable(tmp_path):
    capabilities.registration = False
    app = create_app(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    now = utcnow()
    with TestClient(app) as client:
        with db.SessionLocal() as session:
            monitor = Monitor(
                hostname="pending.example.com",
                registrable_domain="example.com",
                interval_minutes=5,
                screenshot_enabled=True,
                crawl_enabled=False,
                use_proxy=False,
                mobile_ua=False,
                created_at=now,
                updated_at=now,
                last_partial=False,
            )
            session.add(monitor)
            session.commit()
            monitor_id = monitor.id
        page = client.get(f"/domains/{monitor_id}")
        assert page.status_code == 200
        assert "Registration lookups are unavailable." in page.text
        assert "Registration lookup has not completed yet." not in page.text
        assert "disabled" in page.text
        assert "Chromium is not installed." in page.text or "Screenshots are unavailable." in page.text
