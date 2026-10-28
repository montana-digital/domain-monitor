import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.checks.types import UNEXPECTED

logger = logging.getLogger(__name__)


@dataclass
class RegistrationRecord:
    success: bool
    error: str | None = None
    source: str | None = None
    registrar: str | None = None
    created_date: datetime | None = None
    updated_date: datetime | None = None
    expires_date: datetime | None = None
    nameservers: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)


@dataclass
class RegistrationView:
    pending: bool
    failed: bool
    error: str | None
    fetched_at: datetime | None
    success_at: datetime | None
    record: object | None


def lookup_registration(domain: str, *, rdap, whois_lookup) -> RegistrationRecord:
    rdap_error = "RDAP returned no data."
    try:
        record = rdap(domain)
        if record is not None and record.success:
            return record
        if record is not None and record.error:
            rdap_error = record.error
    except Exception as exc:
        logger.info("RDAP lookup failed for %s", domain)
        rdap_error = _short(exc)
    whois_error = "WHOIS returned no data."
    try:
        record = whois_lookup(domain)
        if record is not None and record.success:
            return record
        if record is not None and record.error:
            whois_error = record.error
    except Exception as exc:
        logger.info("WHOIS lookup failed for %s", domain)
        whois_error = _short(exc)
    return RegistrationRecord(False, f"RDAP failed: {rdap_error} WHOIS failed: {whois_error}")


def registration_view(snapshots: list) -> RegistrationView:
    if not snapshots:
        return RegistrationView(True, False, None, None, None, None)
    latest = snapshots[0]
    success = next((item for item in snapshots if item.success), None)
    return RegistrationView(
        pending=False,
        failed=not latest.success,
        error=latest.error,
        fetched_at=latest.fetched_at,
        success_at=None if success is None else success.fetched_at,
        record=success,
    )


def rdap_lookup(domain: str) -> RegistrationRecord:
    import whoisit

    bootstrapped = getattr(whoisit, "is_bootstrapped", None)
    if not callable(bootstrapped) or not bootstrapped():
        whoisit.bootstrap()
    data = whoisit.domain(domain)
    record = _from_mapping(
        data if isinstance(data, dict) else {},
        source="rdap",
        created_keys=("registration_date", "created"),
        updated_keys=("last_changed_date", "updated"),
        expires_keys=("expiration_date", "expires"),
        registrar_keys=("registrar",),
        nameserver_keys=("nameservers",),
        status_keys=("status",),
    )
    if not record.success:
        raise RuntimeError(record.error or "RDAP returned no registration data.")
    return record


def whois_lookup(domain: str) -> RegistrationRecord:
    import whois

    data = whois.whois(domain)
    if data is None:
        return RegistrationRecord(False, "WHOIS returned no registration data.", source="whois")
    mapping = data if isinstance(data, dict) else getattr(data, "__dict__", {})
    if hasattr(data, "get"):
        mapping = data
    return _from_mapping(
        mapping,
        source="whois",
        created_keys=("creation_date",),
        updated_keys=("updated_date",),
        expires_keys=("expiration_date",),
        registrar_keys=("registrar",),
        nameserver_keys=("name_servers", "nameservers"),
        status_keys=("status",),
    )


def _from_mapping(data, *, source: str, created_keys, updated_keys, expires_keys, registrar_keys, nameserver_keys, status_keys) -> RegistrationRecord:
    getter = data.get if hasattr(data, "get") else lambda key, default=None: default
    created = _date(getter(_first_present(data, created_keys)))
    updated = _date(getter(_first_present(data, updated_keys)))
    expires = _date(getter(_first_present(data, expires_keys)))
    registrar = _text(getter(_first_present(data, registrar_keys)))
    nameservers = _strings(getter(_first_present(data, nameserver_keys)))
    statuses = _strings(getter(_first_present(data, status_keys)))
    if not any((registrar, created, updated, expires, nameservers, statuses)):
        return RegistrationRecord(False, "The registry returned no registration data.", source=source)
    return RegistrationRecord(
        True,
        source=source,
        registrar=registrar,
        created_date=created,
        updated_date=updated,
        expires_date=expires,
        nameservers=nameservers,
        statuses=statuses,
    )


def _first_present(data, keys: tuple[str, ...]) -> str | None:
    getter = data.get if hasattr(data, "get") else None
    if getter is None:
        return None
    for key in keys:
        if getter(key) not in (None, "", [], ()):
            return key
    return None


def _date(value):
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, datetime):
        return value
    return None


def _text(value) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text[:300] or None


def _strings(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    else:
        try:
            items = list(value)
        except TypeError:
            items = [value]
    cleaned = []
    seen = set()
    for item in items:
        if isinstance(item, dict):
            item = item.get("name") or item.get("ldhName") or item.get("nameserver") or ""
        text = str(item).strip().lower().rstrip(".")
        if text and text not in seen:
            seen.add(text)
            cleaned.append(text)
    return cleaned


def _short(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if text == UNEXPECTED:
        return text
    return text[:240]
