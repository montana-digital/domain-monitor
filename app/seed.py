from datetime import timedelta

from sqlalchemy import select

from app.attention import DEFAULT_WATCH_DAYS, WATCH_INTERVAL, routine_interval
from app.clock import utcnow
from app.config import current
from app.domain_parser import parse_domain
from app.models import AppSetting, Monitor

# Public hostnames used once so the 10-row pages can be tried.
# Status checks only. Deleted rows are not put back.
SAMPLE_WATCH = (
    "example.com",
    "example.org",
    "example.net",
    "wikipedia.org",
    "en.wikipedia.org",
    "wikimedia.org",
    "mozilla.org",
    "www.mozilla.org",
    "cloudflare.com",
    "www.cloudflare.com",
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "python.org",
    "www.python.org",
    "pypi.org",
    "debian.org",
    "ubuntu.com",
    "kernel.org",
    "apache.org",
    "nginx.org",
    "postgresql.org",
    "sqlite.org",
    "iana.org",
    "icann.org",
    "archive.org",
    "www.archive.org",
    "eff.org",
    "creativecommons.org",
    "w3.org",
    "www.w3.org",
    "whatwg.org",
    "developer.mozilla.org",
    "docs.python.org",
    "npmjs.com",
    "crates.io",
    "golang.org",
    "rust-lang.org",
    "openjdk.org",
    "linux.org",
)

# (hostname, days since last activity, days already on the routine ladder)
SAMPLE_ROUTINE = (
    ("www.bbc.co.uk", 140, 120),
    ("bbc.com", 130, 100),
    ("news.ycombinator.com", 80, 70),
    ("stackoverflow.com", 60, 50),
    ("reuters.com", 45, 40),
    ("nytimes.com", 20, 20),
    ("theguardian.com", 12, 12),
    ("apnews.com", 6, 6),
    ("nature.com", 3, 3),
    ("sciencemag.org", 1, 1),
)


def seed_sample_domains() -> None:
    from app import db

    if db.SessionLocal is None or not current().seed_sample:
        return
    now = utcnow()
    with db.SessionLocal() as session:
        setting = session.get(AppSetting, 1)
        if setting is None or setting.sample_seeded:
            return
        existing = {host.lower() for host in session.scalars(select(Monitor.hostname)).all()}
        for hostname in SAMPLE_WATCH:
            _add_watch(session, hostname, existing, now)
        for hostname, quiet_days, routine_days in SAMPLE_ROUTINE:
            _add_routine(session, hostname, quiet_days, routine_days, existing, now)
        setting.sample_seeded = True
        db.commit_with_retry(session)


def _add_watch(session, hostname: str, existing: set[str], now) -> None:
    parsed = parse_domain(hostname)
    if parsed.error or not parsed.hostname or parsed.hostname.lower() in existing:
        return
    session.add(
        Monitor(
            hostname=parsed.hostname,
            registrable_domain=parsed.registrable_domain,
            interval_minutes=WATCH_INTERVAL,
            screenshot_enabled=False,
            crawl_enabled=False,
            use_proxy=False,
            mobile_ua=False,
            attention="watch",
            watch_days=DEFAULT_WATCH_DAYS,
            watch_until=now + timedelta(days=DEFAULT_WATCH_DAYS),
            quiet_since=now,
            created_at=now,
            updated_at=now,
            next_check_at=now,
            last_partial=False,
        )
    )
    existing.add(parsed.hostname.lower())


def _add_routine(session, hostname: str, quiet_days: int, routine_days: int, existing: set[str], now) -> None:
    parsed = parse_domain(hostname)
    if parsed.error or not parsed.hostname or parsed.hostname.lower() in existing:
        return
    routine_since = now - timedelta(days=routine_days)
    session.add(
        Monitor(
            hostname=parsed.hostname,
            registrable_domain=parsed.registrable_domain,
            interval_minutes=routine_interval(routine_since, now),
            screenshot_enabled=False,
            crawl_enabled=False,
            use_proxy=False,
            mobile_ua=False,
            attention="routine",
            watch_days=DEFAULT_WATCH_DAYS,
            quiet_since=now - timedelta(days=quiet_days),
            routine_since=routine_since,
            created_at=now - timedelta(days=quiet_days),
            updated_at=now,
            next_check_at=now,
            last_partial=False,
        )
    )
    existing.add(parsed.hostname.lower())
