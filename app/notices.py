from datetime import timedelta

from app.clock import as_utc, utcnow

LOGIN_LIMIT = 5
LOGIN_WINDOW = timedelta(minutes=15)


class RuntimeNotices:
    def __init__(self) -> None:
        self.started_at = utcnow()
        self.last_tick = None
        self.browser_error: str | None = None
        self.disk_error: str | None = None
        self.registration_unavailable: str | None = None
        self.scheduler_enabled = True
        self.login_failures: dict[str, list] = {}

    def login_throttled(self, client_host: str, now=None) -> bool:
        now = now or utcnow()
        recent = self._recent_failures(client_host, now)
        self.login_failures[client_host] = recent
        return len(recent) >= LOGIN_LIMIT

    def record_login_failure(self, client_host: str, now=None) -> None:
        now = now or utcnow()
        recent = self._recent_failures(client_host, now)
        recent.append(now)
        self.login_failures[client_host] = recent

    def clear_login_failures(self, client_host: str) -> None:
        self.login_failures.pop(client_host, None)

    def _recent_failures(self, client_host: str, now) -> list:
        return [item for item in self.login_failures.get(client_host, []) if now - item < LOGIN_WINDOW]

    def scheduler_stalled(self, now=None) -> bool:
        if not self.scheduler_enabled:
            return False
        now = now or utcnow()
        tick = as_utc(self.last_tick)
        if tick is None:
            return now - self.started_at > timedelta(seconds=60)
        return now - tick > timedelta(seconds=60)

    def banners(self) -> list[str]:
        items: list[str] = []
        if self.registration_unavailable:
            items.append(self.registration_unavailable)
        if self.browser_error:
            items.append(f"Screenshots are unavailable: {self.browser_error}")
        if self.disk_error:
            items.append(f"Screenshot storage failed: {self.disk_error}")
        if self.scheduler_stalled():
            items.append("Scheduler has not run in over a minute.")
        return items

    def reset(self) -> None:
        self.started_at = utcnow()
        self.last_tick = None
        self.browser_error = None
        self.disk_error = None
        self.registration_unavailable = None
        self.scheduler_enabled = True
        self.login_failures = {}


notices = RuntimeNotices()
