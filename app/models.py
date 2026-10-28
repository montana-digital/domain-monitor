from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Monitor(Base):
    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    hostname: Mapped[str] = mapped_column(String(253), unique=True, index=True)
    registrable_domain: Mapped[str] = mapped_column(String(253))
    interval_minutes: Mapped[int] = mapped_column(Integer, default=5)
    screenshot_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    crawl_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    use_proxy: Mapped[bool] = mapped_column(Boolean, default=False)
    mobile_ua: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_partial: Mapped[bool] = mapped_column(Boolean, default=False)
    last_partial_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_scheme: Mapped[str | None] = mapped_column(String(8), nullable=True)

    events: Mapped[list["CheckEvent"]] = relationship(
        back_populates="monitor",
        cascade="all, delete-orphan",
        order_by="CheckEvent.id",
    )
    snapshots: Mapped[list["RegistrationSnapshot"]] = relationship(
        back_populates="monitor",
        cascade="all, delete-orphan",
        order_by="RegistrationSnapshot.id",
    )


class Proxy(Base):
    __tablename__ = "proxies"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    retention_days: Mapped[int] = mapped_column(Integer, default=30)


class RegistrationSnapshot(Base):
    __tablename__ = "registration_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    success: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    registrar: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    nameservers: Mapped[str | None] = mapped_column(Text, nullable=True)
    statuses: Mapped[str | None] = mapped_column(Text, nullable=True)

    monitor: Mapped[Monitor] = relationship(back_populates="snapshots")


class CheckEvent(Base):
    __tablename__ = "check_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    monitor_id: Mapped[int] = mapped_column(ForeignKey("monitors.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    partial: Mapped[bool] = mapped_column(Boolean, default=False)
    partial_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    scheme: Mapped[str | None] = mapped_column(String(8), nullable=True)
    screenshot_outcome: Mapped[str] = mapped_column(String(16), default="skipped")
    screenshot_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    crawl_outcome: Mapped[str] = mapped_column(String(16), default="skipped")
    crawl_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    crawl_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    crawl_saved: Mapped[int] = mapped_column(Integer, default=0)
    crawl_failed: Mapped[int] = mapped_column(Integer, default=0)

    monitor: Mapped[Monitor] = relationship(back_populates="events")
    attempts: Mapped[list["CheckAttempt"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="CheckAttempt.id",
    )
    pages: Mapped[list["CrawlPage"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="CrawlPage.id",
    )
    screenshots: Mapped[list["Screenshot"]] = relationship(
        back_populates="event",
        cascade="all, delete-orphan",
        order_by="Screenshot.id",
    )


class CheckAttempt(Base):
    __tablename__ = "check_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    check_event_id: Mapped[int] = mapped_column(ForeignKey("check_events.id", ondelete="CASCADE"), index=True)
    vantage: Mapped[str] = mapped_column(String(80))
    proxy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proxy_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    profile: Mapped[str] = mapped_column(String(16))
    scheme: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_modified: Mapped[str | None] = mapped_column(Text, nullable=True)

    event: Mapped[CheckEvent] = relationship(back_populates="attempts")


class CrawlPage(Base):
    __tablename__ = "crawl_pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    check_event_id: Mapped[int] = mapped_column(ForeignKey("check_events.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(Text)
    depth: Mapped[int] = mapped_column(Integer)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)

    event: Mapped[CheckEvent] = relationship(back_populates="pages")


class Screenshot(Base):
    __tablename__ = "screenshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    check_event_id: Mapped[int] = mapped_column(ForeignKey("check_events.id", ondelete="CASCADE"), index=True)
    viewport: Mapped[str] = mapped_column(String(16))
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    event: Mapped[CheckEvent] = relationship(back_populates="screenshots")
