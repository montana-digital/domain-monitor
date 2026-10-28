from dataclasses import dataclass, field


@dataclass
class HttpResult:
    status_code: int | None = None
    error: str | None = None
    scheme: str | None = None
    final_url: str | None = None
    latency_ms: int | None = None
    last_modified: str | None = None


@dataclass
class ProxySpec:
    id: int
    label: str
    url: str


@dataclass
class AttemptRecord:
    vantage: str
    proxy_id: int | None
    proxy_label: str | None
    proxy_url: str | None
    profile: str
    scheme: str | None
    status_code: int | None
    error: str | None
    latency_ms: int | None
    final_url: str | None
    last_modified: str | None


@dataclass
class Shot:
    viewport: str
    data: bytes | None = None
    error: str | None = None


@dataclass
class PageRecord:
    url: str
    depth: int
    status_code: int | None
    error: str | None
    title: str | None


@dataclass
class CrawlResult:
    outcome: str
    error: str | None = None
    note: str | None = None
    saved: int = 0
    failed: int = 0
    pages: list[PageRecord] = field(default_factory=list)


@dataclass
class CheckOutcome:
    status_code: int | None
    error: str | None
    scheme: str | None
    partial: bool
    partial_reason: str | None
    attempts: list[AttemptRecord]
    screenshot_outcome: str
    screenshot_error: str | None
    shots: list[Shot]
    crawl_outcome: str
    crawl_error: str | None
    crawl_note: str | None
    crawl_saved: int
    crawl_failed: int
    pages: list[PageRecord]


UNEXPECTED = "Check failed: unexpected error"
