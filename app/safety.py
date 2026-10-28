import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
DNS_TIMEOUT = 5


def is_blocked_ip(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return True
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    ):
        return True
    if ip.version == 4 and ip in _CGNAT:
        return True
    return False


def resolve_public_sync(hostname: str) -> tuple[str | None, list[str]]:
    host = (hostname or "").strip().rstrip(".").lower()
    if not host or host in {"localhost", "localhost.localdomain"}:
        return "Private address", []
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if is_blocked_ip(host):
            return "Private address", []
        return None, [host]
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return "DNS failed", []
    addresses: list[str] = []
    for item in infos:
        address = item[4][0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        return "DNS failed", []
    if any(is_blocked_ip(address) for address in addresses):
        return "Private address", []
    return None, addresses


def public_hostname_error_sync(hostname: str) -> str | None:
    error, _addresses = resolve_public_sync(hostname)
    return error


async def resolve_public(hostname: str) -> tuple[str | None, list[str]]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(resolve_public_sync, hostname),
            DNS_TIMEOUT,
        )
    except TimeoutError:
        return "DNS failed", []


async def public_hostname_error(hostname: str) -> str | None:
    error, _addresses = await resolve_public(hostname)
    return error


def parse_proxy_url(raw: str) -> tuple[str | None, str | None]:
    text = (raw or "").strip()
    if not text:
        return None, "Enter a proxy URL."
    parts = urlsplit(text)
    if parts.scheme in {"socks", "socks4", "socks5", "socks5h"}:
        return None, "Use an HTTP or HTTPS proxy URL. Authenticated SOCKS proxies are not supported."
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None, "Proxy URL must start with http:// or https://."
    return text, None


def proxy_label(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or "proxy"
    if parts.port:
        return f"{host}:{parts.port}"
    return host
