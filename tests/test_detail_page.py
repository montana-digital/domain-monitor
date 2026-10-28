import re
import shutil
from datetime import timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import db
from app.clock import utcnow
from app.config import Settings
from app.main import create_app
from app.models import CheckAttempt, CheckEvent, CrawlPage, Monitor, Screenshot
from app.service import retain_screenshots
from app.presentation import format_dt, profiles_used, screenshot_mark


def test_screenshot_mark_tracks_whether_the_file_is_still_stored():
    saved = SimpleNamespace(path="screenshots/1-desktop.jpg")
    removed = SimpleNamespace(path=None)

    assert screenshot_mark("succeeded", [saved]) == "saved"
    assert screenshot_mark("succeeded", [removed, saved]) == "saved"
    assert screenshot_mark("succeeded", [removed]) == "removed"
    assert screenshot_mark("succeeded", []) == "removed"
    assert screenshot_mark("failed", [saved]) == "missing"
    assert screenshot_mark("skipped", []) == "missing"
    assert screenshot_mark(None, None) == "missing"


def test_profiles_used_reports_desktop_and_mobile_only():
    desktop = SimpleNamespace(profile="desktop")
    mobile = SimpleNamespace(profile="mobile")
    mobile_shot = SimpleNamespace(viewport="mobile")

    assert profiles_used([desktop], []) == ["desktop"]
    assert profiles_used([desktop], [mobile_shot]) == ["desktop", "mobile"]
    assert profiles_used([mobile, desktop], []) == ["desktop", "mobile"]
    assert profiles_used([], []) == []
    assert profiles_used([SimpleNamespace(profile="tablet")], []) == []


def test_detail_page_folds_are_closed_and_summarize_the_check(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    started = utcnow()
    with TestClient(app) as client:
        with db.SessionLocal() as session:
            monitor = Monitor(
                hostname="example.com",
                registrable_domain="example.com",
                interval_minutes=5,
                screenshot_enabled=True,
                crawl_enabled=True,
                use_proxy=False,
                mobile_ua=True,
                created_at=started,
                updated_at=started,
                last_partial=False,
            )
            session.add(monitor)
            session.flush()
            event = CheckEvent(
                monitor_id=monitor.id,
                trigger="schedule",
                started_at=started,
                finished_at=started,
                status_code=200,
                scheme="https",
                partial=False,
                screenshot_outcome="succeeded",
                crawl_outcome="partial",
                crawl_saved=2,
                crawl_failed=1,
            )
            session.add(event)
            session.flush()
            session.add_all(
                [
                    CheckAttempt(
                        check_event_id=event.id,
                        vantage="Direct desktop",
                        profile="desktop",
                        scheme="https",
                        status_code=200,
                        latency_ms=40,
                        final_url="https://example.com/",
                    ),
                    CheckAttempt(
                        check_event_id=event.id,
                        vantage="Direct mobile",
                        profile="mobile",
                        scheme="https",
                        status_code=200,
                        latency_ms=55,
                        final_url="https://example.com/",
                    ),
                    Screenshot(
                        check_event_id=event.id,
                        viewport="desktop",
                        path="screenshots/1-desktop.jpg",
                        created_at=started,
                    ),
                ]
            )
            retained = CheckEvent(
                monitor_id=monitor.id,
                trigger="schedule",
                started_at=started - timedelta(minutes=5),
                finished_at=started - timedelta(minutes=5),
                status_code=200,
                scheme="https",
                partial=False,
                screenshot_outcome="succeeded",
                crawl_outcome="succeeded",
                crawl_saved=4,
                crawl_failed=0,
            )
            failed = CheckEvent(
                monitor_id=monitor.id,
                trigger="manual",
                started_at=started - timedelta(minutes=10),
                finished_at=started - timedelta(minutes=10),
                status_code=500,
                scheme="https",
                partial=True,
                partial_reason="Screenshot failed",
                screenshot_outcome="failed",
                screenshot_error="Browser unavailable",
                crawl_outcome="skipped",
            )
            session.add_all([retained, failed])
            session.flush()
            session.add(
                Screenshot(
                    check_event_id=retained.id,
                    viewport="desktop",
                    path=None,
                    error="Image removed by retention.",
                    created_at=started - timedelta(minutes=5),
                )
            )
            session.commit()
            monitor_id = monitor.id

        page = client.get(f"/domains/{monitor_id}")
        assert page.status_code == 200
        html = page.text
        stamp = format_dt(started)
        assert html.index("WHOIS / RDAP") < html.index("Setup for example.com")
        assert html.index("Setup for example.com") < html.index("Check history") < html.index(stamp)
        assert html.index(stamp) < html.index('id="delete-heading"')
        assert html.count('<details class="fold">') == 2
        assert '<details class="fold event">' in html
        assert not re.search(r'<details class="fold(?: event)?"[^>]*\bopen\b', html)
        assert '<details class="fold danger-zone">' in html
        assert "Screenshot saved" in html
        assert "Screenshot removed" in html
        assert "No screenshot" in html
        assert "Crawl saved" in html
        assert "Crawl partial" in html
        assert '<span class="sr-only">Desktop</span>' in html
        assert '<span class="sr-only">Mobile</span>' in html

        plain = Monitor(
            hostname="plain.example.com",
            registrable_domain="example.com",
            interval_minutes=5,
            screenshot_enabled=False,
            crawl_enabled=False,
            use_proxy=False,
            mobile_ua=False,
            created_at=started,
            updated_at=started,
            last_partial=False,
        )
        with db.SessionLocal() as session:
            session.add(plain)
            session.flush()
            plain_event = CheckEvent(
                monitor_id=plain.id,
                trigger="schedule",
                started_at=started,
                finished_at=started,
                status_code=204,
                scheme="https",
                partial=False,
                screenshot_outcome="skipped",
                crawl_outcome="skipped",
            )
            session.add(plain_event)
            session.flush()
            session.add(
                CheckAttempt(
                    check_event_id=plain_event.id,
                    vantage="Direct desktop",
                    profile="desktop",
                    scheme="https",
                    status_code=204,
                    final_url="https://plain.example.com/",
                )
            )
            session.commit()
            plain_id = plain.id

        quiet = client.get(f"/domains/{plain_id}")
        assert quiet.status_code == 200
        assert "Screenshot saved" not in quiet.text
        assert "No screenshot" not in quiet.text
        assert "Crawl saved" not in quiet.text
        assert "Crawl partial" not in quiet.text
        assert "No crawl" not in quiet.text
        assert '<span class="sr-only">Desktop</span>' in quiet.text
        assert '<span class="sr-only">Mobile</span>' not in quiet.text

        confirm = client.get(f"/domains/{monitor_id}?confirm_delete=1")
        assert confirm.status_code == 200
        assert '<details class="fold danger-zone" open>' in confirm.text
        assert not re.search(r'<details class="fold event"[^>]*\bopen\b', confirm.text)


def test_retention_drops_old_events_and_keeps_recent_history(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    now = utcnow()
    with TestClient(app) as client:
        with db.SessionLocal() as session:
            monitor = Monitor(
                hostname="history.example.com",
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
            session.flush()
            old = CheckEvent(
                monitor_id=monitor.id,
                trigger="schedule",
                started_at=now - timedelta(days=40),
                finished_at=now - timedelta(days=40),
                error="OLD-CHECK-GONE",
                partial=False,
                screenshot_outcome="succeeded",
                crawl_outcome="skipped",
            )
            recent = CheckEvent(
                monitor_id=monitor.id,
                trigger="schedule",
                started_at=now,
                finished_at=now,
                status_code=200,
                scheme="https",
                partial=False,
                screenshot_outcome="succeeded",
                crawl_outcome="skipped",
            )
            session.add_all([old, recent])
            session.flush()
            session.add(
                CrawlPage(
                    check_event_id=old.id,
                    url="https://history.example.com/old",
                    depth=1,
                    status_code=200,
                    title="Old page",
                )
            )
            old_file = tmp_path / "screenshots" / "old.jpg"
            recent_file = tmp_path / "screenshots" / "recent.jpg"
            old_file.write_bytes(b"old-image")
            recent_file.write_bytes(b"new-image")
            session.add(
                Screenshot(
                    check_event_id=old.id,
                    viewport="desktop",
                    path="old.jpg",
                    created_at=now - timedelta(days=40),
                )
            )
            session.add(
                Screenshot(
                    check_event_id=recent.id,
                    viewport="desktop",
                    path="recent.jpg",
                    created_at=now,
                )
            )
            session.commit()
            monitor_id = monitor.id
            old_id = old.id
        retain_screenshots()
        page = client.get(f"/domains/{monitor_id}")
        assert page.status_code == 200
        assert "OLD-CHECK-GONE" not in page.text
        assert ">200<" in page.text
        assert not old_file.exists()
        assert recent_file.is_file()
        with db.SessionLocal() as session:
            assert session.get(CheckEvent, old_id) is None
            assert session.query(CrawlPage).count() == 0
            remaining = session.query(CheckEvent).filter(CheckEvent.monitor_id == monitor_id).count()
            assert remaining == 1


def test_screenshot_paths_follow_the_data_directory(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, password=None, secret_key="test-secret", start_scheduler=False))
    now = utcnow()
    with TestClient(app) as client:
        with db.SessionLocal() as session:
            monitor = Monitor(
                hostname="shots.example.com",
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
            session.flush()
            event = CheckEvent(
                monitor_id=monitor.id,
                trigger="manual",
                started_at=now,
                finished_at=now,
                status_code=200,
                partial=False,
                screenshot_outcome="succeeded",
                crawl_outcome="skipped",
            )
            session.add(event)
            session.flush()
            relative = tmp_path / "screenshots" / "kept.jpg"
            legacy = tmp_path / "screenshots" / "legacy.jpg"
            outside = tmp_path / "secret.jpg"
            relative.write_bytes(b"kept-image")
            legacy.write_bytes(b"legacy-image")
            outside.write_bytes(b"secret-image")
            kept = Screenshot(check_event_id=event.id, viewport="desktop", path="kept.jpg", created_at=now)
            absolute = Screenshot(
                check_event_id=event.id,
                viewport="mobile",
                path=str(legacy.resolve()),
                created_at=now,
            )
            escaped = Screenshot(
                check_event_id=event.id,
                viewport="desktop",
                path=str(outside.resolve()),
                created_at=now,
            )
            session.add_all([kept, absolute, escaped])
            session.commit()
            kept_id = kept.id
            absolute_id = absolute.id
            escaped_id = escaped.id
        kept_response = client.get(f"/screenshots/{kept_id}")
        assert kept_response.status_code == 200
        assert kept_response.content == b"kept-image"
        rewritten = client.get(f"/screenshots/{absolute_id}")
        assert rewritten.status_code == 200
        assert rewritten.content == b"legacy-image"
        blocked = client.get(f"/screenshots/{escaped_id}")
        assert blocked.status_code == 404
        assert outside.is_file()
        with db.SessionLocal() as session:
            assert session.get(Screenshot, absolute_id).path == "legacy.jpg"
            assert session.get(Screenshot, escaped_id).path == str(outside.resolve())

    moved = tmp_path / "moved"
    moved.mkdir()
    for name in ("monitor.db", "monitor.db-wal", "monitor.db-shm"):
        source = tmp_path / name
        if source.exists():
            shutil.copy2(source, moved / name)
    shutil.copytree(tmp_path / "screenshots", moved / "screenshots")
    moved_app = create_app(Settings(data_dir=moved, password=None, secret_key="test-secret", start_scheduler=False))
    with TestClient(moved_app) as client:
        followed = client.get(f"/screenshots/{kept_id}")
        assert followed.status_code == 200
        assert followed.content == b"kept-image"
