from app.domain_parser import parse_csv, parse_domain
from app.safety import is_blocked_ip, parse_proxy_url


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
    assert by_line[3].error == "Unknown public suffix."
    assert by_line[4].hostname == "www.bbc.co.uk"
    assert by_line[4].crawl is True
    assert by_line[4].mobile_ua is True
    assert by_line[5].error == "This domain is already in the list."
    assert "Interval" in by_line[6].error
    again = parse_csv("taken.com\n", existing={"taken.com"})
    assert again.rows[0].error == "This domain is already in the list."


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
    assert not is_blocked_ip("8.8.8.8")
    assert not is_blocked_ip("1.1.1.1")
    assert parse_proxy_url("socks5://user:pass@proxy.example:1080")[1]
    assert parse_proxy_url("http://proxy.example:8080")[0] == "http://proxy.example:8080"
