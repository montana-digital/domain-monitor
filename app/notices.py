from datetime import timedelta

from app.clock import utcnow


class RuntimeNotices:
    def __init__(self) -> None:
        self.started_at = utcnow()
        self.last_tick = None
        self.browser_error: str | None = None
        self.disk_error: str | None = None
        self.registration_error: str | None = None
        self.scheduler_enabled = True

    def banners(self) -> list[str]:
        now = utcnow()
        items: list[str] = []
        if self.browser_error:
            items.append(f"Screenshots are unavailable: {self.browser_error}")
        if self.disk_error:
            items.append(f"Screenshot storage failed: {self.disk_error}")
        if self.registration_error:
            items.append(f"Registration lookups are failing: {self.registration_error}")
        if self.scheduler_enabled:
            tick = self.last_tick
            stalled = tick is None and now - self.started_at > timedelta(seconds=60)
            stalled = stalled or (tick is not None and now - tick > timedelta(seconds=60))
            if stalled:
                items.append("Scheduler has not run in over a minute.")
        return items

    def reset(self) -> None:
        self.started_at = utcnow()
        self.last_tick = None
        self.browser_error = None
        self.disk_error = None
        self.registration_error = None
        self.scheduler_enabled = True


notices = RuntimeNotices()
