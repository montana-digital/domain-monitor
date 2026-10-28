import csv
import io
import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import tldextract

ALLOWED_INTERVALS = {1, 5, 15, 30, 60}
DEFAULT_INTERVAL = 5
MAX_CSV_ROWS = 500
_LABEL = re.compile(r"[a-z0-9-]+")
_BOOL_TRUE = {"1", "true", "yes", "y", "on"}
_BOOL_FALSE = {"0", "false", "no", "n", "off"}
_CSV_FIELDS = ["domain", "interval_minutes", "screenshot", "crawl", "use_proxy", "mobile_ua"]

_extractor = tldextract.TLDExtract(suffix_list_urls=(), fallback_to_snapshot=True)


@dataclass
class ParsedDomain:
    hostname: str
    registrable_domain: str
    suffix: str
    notes: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class RowResult:
    line: int
    raw: str
    hostname: str | None
    registrable_domain: str | None
    interval_minutes: int
    screenshot: bool
    crawl: bool
    use_proxy: bool
    mobile_ua: bool
    notes: list[str]
    error: str | None


@dataclass
class CsvParse:
    file_error: str | None
    rows: list[RowResult]


def parse_domain(raw: str) -> ParsedDomain:
    notes: list[str] = []
    text = (raw or "").strip()
    if not text:
        return _bad("Enter a domain.")
    if any(character.isspace() for character in text):
        return _bad("Enter a hostname without spaces.")

    hostname, split_notes, error = _hostname_from_input(text)
    notes.extend(split_notes)
    if error:
        return _bad(error, notes)
    if not hostname:
        return _bad("Enter a domain.", notes)

    hostname = hostname.strip(".").lower()
    if not hostname:
        return _bad("Enter a domain.", notes)
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return _bad("IP addresses are not domains.", notes)
    if hostname in {"localhost", "localhost.localdomain"}:
        return _bad("Local hostnames are not public domains.", notes)

    try:
        ascii_host = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return _bad("This hostname cannot be encoded as a domain.", notes)
    if ascii_host != hostname:
        notes.append("Stored the Unicode hostname.")
    if len(ascii_host) > 253:
        return _bad("This hostname is too long.", notes)
    for label in ascii_host.split("."):
        if not label or len(label) > 63 or label.startswith("-") or label.endswith("-"):
            return _bad("This hostname has an invalid label.", notes)
        if _LABEL.fullmatch(label) is None:
            return _bad("This hostname has an invalid label.", notes)

    try:
        canonical = ascii_host.encode("ascii").decode("idna")
    except UnicodeError:
        return _bad("This hostname cannot be encoded as a domain.", notes)

    extracted = _extractor(canonical)
    if not extracted.suffix:
        return _bad("Unknown public suffix.", notes)
    if not extracted.domain:
        return _bad("Enter a registrable domain, not only a public suffix.", notes)
    registrable = getattr(extracted, "top_domain_under_public_suffix", None) or getattr(
        extracted, "registered_domain", None
    )
    if not registrable:
        return _bad("Enter a registrable domain, not only a public suffix.", notes)
    return ParsedDomain(
        hostname=canonical,
        registrable_domain=registrable,
        suffix=extracted.suffix,
        notes=notes,
    )


def parse_csv(text: str, existing: set[str] | None = None) -> CsvParse:
    existing_hosts = {item.lower() for item in (existing or set())}
    reader = csv.reader(io.StringIO(text))
    raw_rows = list(reader)
    while raw_rows and _blank_row(raw_rows[-1]):
        raw_rows.pop()
    if not raw_rows:
        return CsvParse("No domains found in the CSV.", [])

    start = 0
    header = None
    first = [cell.strip() for cell in raw_rows[0]]
    if first and first[0].lower() == "domain":
        header = [cell.strip().lower() for cell in raw_rows[0]]
        start = 1
    data_rows = [(index, row) for index, row in enumerate(raw_rows[start:], start=start + 1) if not _blank_row(row)]
    if not data_rows:
        return CsvParse("No domains found in the CSV.", [])
    if len(data_rows) > MAX_CSV_ROWS:
        return CsvParse(
            f"CSV has more than {MAX_CSV_ROWS} domains. Split the file and import again.",
            [],
        )

    seen: set[str] = set()
    rows: list[RowResult] = []
    for line, cells in data_rows:
        raw_domain = _cell(cells, header, "domain", 0)
        interval, interval_error = _interval(_cell(cells, header, "interval_minutes", 1))
        screenshot, screenshot_error = _bool_cell(_cell(cells, header, "screenshot", 2), False)
        crawl, crawl_error = _bool_cell(_cell(cells, header, "crawl", 3), False)
        use_proxy, proxy_error = _bool_cell(_cell(cells, header, "use_proxy", 4), False)
        mobile, mobile_error = _bool_cell(_cell(cells, header, "mobile_ua", 5), False)
        field_error = interval_error or screenshot_error or crawl_error or proxy_error or mobile_error
        parsed = parse_domain(raw_domain)
        error = field_error or parsed.error
        hostname = parsed.hostname if not parsed.error else None
        if error is None and hostname is not None:
            key = hostname.lower()
            if key in existing_hosts or key in seen:
                error = "This domain is already in the list."
                hostname = None
            else:
                seen.add(key)
        rows.append(
            RowResult(
                line=line,
                raw=raw_domain,
                hostname=hostname,
                registrable_domain=parsed.registrable_domain if hostname else None,
                interval_minutes=interval,
                screenshot=screenshot,
                crawl=crawl,
                use_proxy=use_proxy,
                mobile_ua=mobile,
                notes=list(parsed.notes),
                error=error,
            )
        )
    return CsvParse(None, rows)


def _bad(message: str, notes: list[str] | None = None) -> ParsedDomain:
    return ParsedDomain("", "", "", notes or [], message)


def _hostname_from_input(text: str) -> tuple[str | None, list[str], str | None]:
    notes: list[str] = []
    if "://" in text or text.startswith("//"):
        parsed = urlsplit(text if "://" in text else f"https:{text}")
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            return None, notes, "Only http and https URLs can be monitored."
        if parsed.scheme:
            notes.append("Removed the URL scheme.")
        if parsed.username or parsed.password:
            notes.append("Removed user info.")
        if parsed.port:
            notes.append(f"Removed port {parsed.port}.")
        if parsed.path not in {"", "/"}:
            notes.append("Removed the URL path.")
        if parsed.query:
            notes.append("Removed the query string.")
        if parsed.fragment:
            notes.append("Removed the fragment.")
        return parsed.hostname, notes, None

    bare = text
    if "@" in bare:
        notes.append("Removed user info.")
        bare = bare.rsplit("@", 1)[-1]
    if "/" in bare:
        host, path = bare.split("/", 1)
        bare = host
        if path:
            notes.append("Removed the URL path.")
    if "?" in bare:
        bare, _query = bare.split("?", 1)
        notes.append("Removed the query string.")
    if bare.count(":") == 1:
        host, port = bare.rsplit(":", 1)
        if port.isdigit():
            notes.append(f"Removed port {port}.")
            bare = host
    return bare, notes, None


def _blank_row(row: list[str]) -> bool:
    return not any(cell.strip() for cell in row)


def _cell(cells: list[str], header: list[str] | None, name: str, position: int) -> str:
    if header is None:
        return cells[position].strip() if position < len(cells) else ""
    if name not in header:
        return ""
    index = header.index(name)
    return cells[index].strip() if index < len(cells) else ""


def _interval(value: str) -> tuple[int, str | None]:
    if value == "":
        return DEFAULT_INTERVAL, None
    try:
        minutes = int(value)
    except ValueError:
        return DEFAULT_INTERVAL, f"Interval must be one of {sorted(ALLOWED_INTERVALS)}."
    if minutes not in ALLOWED_INTERVALS:
        return DEFAULT_INTERVAL, f"Interval must be one of {sorted(ALLOWED_INTERVALS)}."
    return minutes, None


def _bool_cell(value: str, default: bool) -> tuple[bool, str | None]:
    if value == "":
        return default, None
    lowered = value.lower()
    if lowered in _BOOL_TRUE:
        return True, None
    if lowered in _BOOL_FALSE:
        return False, None
    return default, f"Expected yes or no, got {value}."
