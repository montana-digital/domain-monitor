import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

_CGNAT = ipaddress.ip_network("100.64.0.0/10")


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


def public_hostname_error_sync(hostname: str) -> str | None:
    host = (hostname or "").strip().rstrip(".").lower()
    if not host or host in {"localhost", "localhost.localdomain"}:
        return "Private address"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return "Private address" if is_blocked_ip(host) else None
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return "DNS failed"
    addresses = {item[4][0] for item in infos}
    if not addresses:
        return "DNS failed"
    if any(is_blocked_ip(address) for address in addresses):
        return "Private address"
    return None


async def public_hostname_error(hostname: str) -> str | None:
    return await asyncio.to_thread(public_hostname_error_sync, hostname)


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
