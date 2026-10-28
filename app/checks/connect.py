import contextvars

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend

from app.safety import resolve_public

address_pins: contextvars.ContextVar[dict[str, list[str]] | None] = contextvars.ContextVar(
    "address_pins",
    default=None,
)


class PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self._inner = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        pins = address_pins.get() or {}
        chosen = pins.get(host) or []
        target = chosen[0] if chosen else host
        return await self._inner.connect_tcp(
            host=target,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        return await self._inner.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def pinned_transport(proxy: str | None) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(proxy=proxy)
    transport._pool._network_backend = PinnedBackend()
    return transport


async def pin_host(host: str) -> str | None:
    if not host:
        return None
    pins = dict(address_pins.get() or {})
    if host in pins:
        return None
    error, addresses = await resolve_public(host)
    if error:
        return error
    pins[host] = addresses
    address_pins.set(pins)
    return None


def open_client(**kwargs) -> httpx.AsyncClient:
    proxy = kwargs.pop("proxy", None)
    kwargs["transport"] = pinned_transport(proxy)
    kwargs.setdefault("trust_env", False)
    return httpx.AsyncClient(**kwargs)
