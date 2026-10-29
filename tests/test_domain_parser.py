import asyncio
import threading
import time

from app.checks.connect import PinnedBackend, address_pins
from app.checks.pin_proxy import start_pin_proxy
from app.domain_parser import parse_csv, parse_domain
from app.safety import DNS_TIMEOUT, is_blocked_ip, parse_proxy_url, public_hostname_error, resolve_public


def test_simple_and_multipart_suffixes():
    google = parse_domain("google.com")
    assert google.error is None
    assert google.hostname == "google.com"
    assert google.registrable_domain == "google.com"
    assert google.suffix == "com"

    bbc = parse_domain("www.bbc.co.uk")
    assert bbc.error is None
    assert bbc.hostname == "www.bbc.co.uk"
    assert bbc.registrable_domain == "bbc.co.uk"
    assert bbc.suffix == "co.uk"

    australia = parse_domain("example.com.au")
    assert australia.error is None
    assert australia.suffix == "com.au"
    assert australia.registrable_domain == "example.com.au"


def test_pasted_url_is_reduced_to_a_hostname():
    parsed = parse_domain("https://user:secret@example.com:8443/docs?q=1#top")
    assert parsed.error is None
    assert parsed.hostname == "example.com"
    assert any("path" in note.lower() for note in parsed.notes)
    assert any("port" in note.lower() for note in parsed.notes)
    assert any("user" in note.lower() for note in parsed.notes)


def test_idna_hostname():
    parsed = parse_domain("münchen.de")
    assert parsed.error is None
    assert parsed.suffix == "de"
    assert parsed.hostname == "münchen.de"
    assert parsed.registrable_domain == "münchen.de"


def test_rejects_unknown_suffix_ip_and_bare_public_suffix():
    assert parse_domain("google.notavalidsuffix").error == "Unknown public suffix."
    assert "IP" in parse_domain("8.8.8.8").error
    assert parse_domain("co.uk").error
    assert parse_domain("localhost").error
    assert parse_domain("not a domain").error


def test_csv_reports_each_bad_row_and_duplicates():
    text = "\n".join(
        [
            "domain,interval_minutes,screenshot,crawl,use_proxy,mobile_ua",
            "google.com,5,yes,no,no,no",
            "google.notavalidsuffix,5,no,no,no,no",
            "www.bbc.co.uk,15,no,yes,no,yes",
            "google.com,5,no,no,no,no",
            "example.com,7,no,no,no,no",
        ]
    )
    parsed = parse_csv(text, existing={"taken.com"})
    assert parsed.file_error is None
    by_line = {row.line: row for row in parsed.rows}
    assert by_line[2].hostname == "google.com"
    assert by_line[2].screenshot is True
    assert by_line[2].interval_minutes == 5
    assert by_line[3].error == "Unknown public suffix."
    assert by_line[4].hostname == "www.bbc.co.uk"
    assert by_line[4].crawl is True
    assert by_line[4].mobile_ua is True
    assert by_line[4].interval_minutes == 15
    assert by_line[5].error == "This domain is already in the list."
    assert by_line[6].error == "Check interval must be one of 5, 15, 30, 60, 240 minutes."
    assert by_line[6].interval_minutes == 30
    again = parse_csv("taken.com\n", existing={"taken.com"})
    assert again.rows[0].error == "This domain is already in the list."


def test_csv_uses_thirty_minutes_when_the_interval_is_absent():
    parsed = parse_csv("domain,screenshot\nexample.com,no\n")
    assert parsed.file_error is None
    assert parsed.rows[0].error is None
    assert parsed.rows[0].interval_minutes == 30
    bare = parse_csv("example.com\n")
    assert bare.rows[0].error is None
    assert bare.rows[0].interval_minutes == 30


def test_csv_rejects_more_than_500_rows():
    text = "\n".join(f"example{index}.com" for index in range(501))
    parsed = parse_csv(text)
    assert parsed.file_error
    assert parsed.rows == []


def test_blocked_addresses_and_proxy_urls():
    assert is_blocked_ip("127.0.0.1")
    assert is_blocked_ip("10.1.1.1")
    assert is_blocked_ip("192.168.1.20")
    assert is_blocked_ip("169.254.169.254")
    assert is_blocked_ip("100.64.0.1")
    assert is_blocked_ip("::1")
    assert is_blocked_ip("::ffff:127.0.0.1")
    assert not is_blocked_ip("8.8.8.8")
    assert not is_blocked_ip("1.1.1.1")
    assert parse_proxy_url("socks5://user:pass@proxy.example:1080")[1]
    assert parse_proxy_url("http://proxy.example:8080")[0] == "http://proxy.example:8080"


def test_dns_lookup_timeout_returns_dns_failed(monkeypatch):
    import socket

    assert DNS_TIMEOUT == 5
    release = threading.Event()

    def hang(*_args, **_kwargs):
        release.wait(timeout=5)
        raise socket.gaierror("late")

    monkeypatch.setattr(socket, "getaddrinfo", hang)
    monkeypatch.setattr("app.safety.DNS_TIMEOUT", 0.05)

    async def scenario():
        started = time.monotonic()
        result = await public_hostname_error("example.com")
        elapsed = time.monotonic() - started
        release.set()
        return result, elapsed

    result, elapsed = asyncio.run(scenario())
    assert result == "DNS failed"
    assert elapsed < 2


def test_resolve_public_returns_the_address_used_for_a_direct_connection(monkeypatch):
    import socket

    def fake(host, port, *args, **kwargs):
        assert host == "example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    error, addresses = asyncio.run(resolve_public("example.com"))
    assert error is None
    assert addresses == ["8.8.8.8"]

    def private(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", private)
    blocked, empty = asyncio.run(resolve_public("example.com"))
    assert blocked == "Private address"
    assert empty == []


def test_direct_connection_uses_the_classified_address():
    seen = []

    class Inner:
        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            seen.append((host, port))
            raise RuntimeError("stopped")

        async def connect_unix_socket(self, path, timeout=None, socket_options=None):
            raise AssertionError("unix")

        async def sleep(self, seconds):
            return None

    backend = PinnedBackend()
    backend._inner = Inner()
    token = address_pins.set({"example.com": ["8.8.8.8"]})

    async def connect():
        try:
            await backend.connect_tcp("example.com", 443)
        except RuntimeError:
            return

    try:
        asyncio.run(connect())
    finally:
        address_pins.reset(token)
    assert seen == [("8.8.8.8", 443)]


def test_pin_proxy_connects_to_the_first_classified_address(monkeypatch):
    import socket

    calls = []

    def fake(host, port, *args, **kwargs):
        if host in {"127.0.0.1", "localhost"}:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port or 0))]
        ip = "8.8.8.8" if not calls else "1.1.1.1"
        calls.append(ip)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    seen = []
    real_open = asyncio.open_connection

    async def fake_open(host, port):
        if host == "127.0.0.1":
            return await real_open(host, port)
        seen.append((host, port))
        raise OSError("stopped")

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    monkeypatch.setattr("app.checks.pin_proxy.asyncio.open_connection", fake_open)

    async def scenario():
        server, port = await start_pin_proxy()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.read(200), 2)
            writer.close()
            return response
        finally:
            server.close()
            await server.wait_closed()

    response = asyncio.run(scenario())
    assert b"502" in response
    assert calls == ["8.8.8.8"]
    assert seen == [("8.8.8.8", 443)]
