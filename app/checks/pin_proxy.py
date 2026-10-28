import asyncio
from urllib.parse import urlsplit

from app.checks.connect import address_pins, pin_host


async def start_pin_proxy() -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(_handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await _proxy_client(reader, writer)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _proxy_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError):
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        return
    line = head.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
    parts = line.split(" ")
    if len(parts) < 2:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        return
    method, target = parts[0].upper(), parts[1]
    if method == "CONNECT":
        parsed = _split_host_port(target, 443)
        if parsed is None:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            return
        await _tunnel(parsed[0], parsed[1], reader, writer)
        return
    url = urlsplit(target)
    if url.scheme not in {"http", "https"} or not url.hostname:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        return
    port = url.port or (443 if url.scheme == "https" else 80)
    await _forward_http(url.hostname, port, url.path or "/", url.query, head, reader, writer)


async def _tunnel(host: str, port: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    remote = await _open_pinned(host, port, writer)
    if remote is None:
        return
    remote_reader, remote_writer = remote
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await _bridge(reader, writer, remote_reader, remote_writer)


async def _forward_http(
    host: str,
    port: int,
    path: str,
    query: str,
    head: bytes,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    remote = await _open_pinned(host, port, writer)
    if remote is None:
        return
    remote_reader, remote_writer = remote
    origin = path or "/"
    if query:
        origin = f"{origin}?{query}"
    lines = head.split(b"\r\n")
    request_line = lines[0].decode("latin1", errors="replace")
    method = request_line.split(" ", 1)[0] or "GET"
    lines[0] = f"{method} {origin} HTTP/1.1".encode("latin1")
    remote_writer.write(b"\r\n".join(lines))
    await remote_writer.drain()
    await _bridge(reader, writer, remote_reader, remote_writer)


async def _open_pinned(host: str, port: int, writer: asyncio.StreamWriter):
    error = await pin_host(host)
    addresses = (address_pins.get() or {}).get(host) or []
    if error or not addresses:
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        return None
    try:
        return await asyncio.open_connection(addresses[0], port)
    except OSError:
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        return None


def _split_host_port(target: str, default: int) -> tuple[str, int] | None:
    text = target.strip()
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return None
        host = text[1:end]
        rest = text[end + 1 :]
        if not rest:
            return host, default
        if not rest.startswith(":"):
            return None
        port_text = rest[1:]
    elif text.count(":") == 1:
        host, port_text = text.split(":", 1)
    else:
        return text, default
    try:
        port = int(port_text)
    except ValueError:
        return None
    if not host or port < 1 or port > 65535:
        return None
    return host, port


async def _bridge(reader, writer, remote_reader, remote_writer) -> None:
    async def copy(source, dest) -> None:
        try:
            while True:
                chunk = await source.read(65536)
                if not chunk:
                    break
                dest.write(chunk)
                await dest.drain()
        except Exception:
            pass
        finally:
            try:
                dest.close()
            except Exception:
                pass

    await asyncio.gather(copy(reader, remote_writer), copy(remote_reader, writer))
