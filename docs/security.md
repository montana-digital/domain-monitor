# Security

This document records the controls the process implements today, and the limits of those controls. It is a description of [app/routes/pages.py](../app/routes/pages.py), [app/main.py](../app/main.py), [app/safety.py](../app/safety.py), and the check modules. It is not a change list.

The app is a single shared password in front of a local tool. The intended deployment is localhost, or a trusted network with the password set. The README tells operators to set `DOMAIN_MONITOR_PASSWORD` before exposing the port, and to keep the bare-metal bind address at `127.0.0.1` unless the port should be reachable.

## Authentication

`require_user` runs for every route on the page router.

| Request | Result |
| --- | --- |
| Path is `/login`, `/health`, or starts with `/static` | Allowed |
| `DOMAIN_MONITOR_PASSWORD` is unset | Allowed |
| Session has `auth` | Allowed |
| Password is set and the session is not authenticated | Redirect to `/login?next={path}` |

`/static` is also mounted outside the router, so assets do not depend on that exception, but the exception is there.

Login compares the submitted password to the environment value with `hmac.compare_digest` on the UTF-8 bytes. A match sets `session["auth"] = True` and redirects to `safe_next` of the posted `next` field. A mismatch renders the login page with status 401 and the text `Incorrect password.` Logout clears the session and sends the browser to `/login` when a password is configured, otherwise to `/`.

There is one shared password. There are no accounts, roles, or password reset. The password is read once at process start. A wrong password is counted for that client address. After 5 failures in 15 minutes, further login posts from that address get status 429 and `Too many login attempts. Try again later.` until the window passes. A successful login clears the count. The throttle is in memory for this process. When the password is unset, the UI does not ask for login, and this throttle is not on the path that serves pages.

`GET /health` does not require a session. It pings the database with `SELECT 1` and reports whether the scheduler has stalled. A healthy process returns `{"status": "ok"}`. A failure returns status 503. Anyone who can reach the port can see that result.

`GET /about` and `GET /fragment/about-network` use the same session rule as the other pages. The network fragment is a fixed direct request to `1.1.1.1` and `2606:4700:4700::1111`. It does not take a URL from the operator. The page shows the machine's public address, the local route address, and the ISP name when registration can look it up. It does not show the password, the session secret, or proxy URLs.

## Session cookie

Starlette `SessionMiddleware` signs the cookie with `settings.secret_key`.

| Setting | Value |
| --- | --- |
| `same_site` | `lax` |
| `https_only` | `False` unless `DOMAIN_MONITOR_HTTPS_ONLY` is `1`, `true`, `yes`, or `on` |
| `max_age` | 14 days |

With the default, the cookie is accepted on plain HTTP. That is the localhost path. Set `DOMAIN_MONITOR_HTTPS_ONLY` when TLS ends at this process, and the cookie is then marked HTTPS-only. A reverse proxy that terminates TLS in front of plain HTTP to this process should leave the setting off, because the app itself is still speaking HTTP.

The secret is `DOMAIN_MONITOR_SECRET` when that variable is set. Otherwise [app/config.py](../app/config.py) reads `DATA_DIR/secret.key`, or creates it with `secrets.token_hex(32)` and tries `chmod 0600`. A failure to chmod is ignored. Replacing or deleting the key signs out every browser. The key file is inside the data directory, which defaults to `./data`.

The session holds `auth`, the CSRF token, and flash messages. Those values are not written to SQLite.

## CSRF

`ensure_csrf` stores a `secrets.token_urlsafe(32)` token in the session on first use and puts it in every rendered form as a hidden `csrf` field. `csrf_ok` requires both the session value and the form value and compares them with `hmac.compare_digest`.

Every state-changing route returns the 403 page when the token is missing or wrong: login, logout, add, CSV preview, CSV import, edit, check now, delete, retention, and proxy add, toggle, and delete. GET routes do not change state.

The token stays the same for the life of the session. Login does not rotate it.

## Open redirects

`safe_next` in [app/presentation.py](../app/presentation.py) accepts a path that starts with `/` and does not start with `//`. Anything else becomes `/`. Login uses it for the query string and for the posted `next` field.

## Response headers and HTML

`SecurityHeaders` adds:

- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: same-origin`

The app does not set `Content-Security-Policy`, `Strict-Transport-Security`, or `X-Frame-Options`.

Jinja autoescape is on in [app/templating.py](../app/templating.py). Page bodies are rendered through that environment. Hostnames, crawl URLs, titles, proxy errors, and registration fields are escaped as text. The CSV import payload is JSON in a hidden field produced by the server from the preview parse, and the import route parses and validates it again.

`<meta name="robots" content="noindex">` is on the base template.

## SSRF controls

The process fetches URLs that originate from operator input (the hostname) and from sites those checks redirect or link to. [app/safety.py](../app/safety.py) is the gate.

`is_blocked_ip` returns true when the address is private, loopback, link-local, reserved, multicast, unspecified, or not global, and when an IPv4 address is in `100.64.0.0/10` (carrier-grade NAT). An unparseable address is blocked.

`public_hostname_error` then:

1. Rejects an empty host, `localhost`, and `localhost.localdomain` as `Private address`.
2. If the host is an IP literal, returns `Private address` when it is blocked, and returns no error when it is a public literal.
3. Resolves the name with `socket.getaddrinfo` for `SOCK_STREAM`. DNS failure is `DNS failed`. If any resolved address is blocked, the result is `Private address`.

That helper runs in a worker thread when called from async code. The async wait is 5 seconds. A resolve that does not return in that time is `DNS failed`.

Where it is applied:

| Stage | Behavior |
| --- | --- |
| HTTP probe, before the client is created | A blocked hostname returns immediately and no request is sent. |
| HTTP probe, httpx request hook | Every request URL, including each redirect hop, is checked. A blocked host raises `PrivateTarget`, which is stored as `Private address`. |
| Crawl fetch and each manual redirect | The host is checked, with a per-crawl cache. A blocked redirect is stored on that page and not followed. |
| Screenshot, before launch | The capture URL host is checked. |
| Screenshot, Playwright route on `**/*` | Each request URL is checked. A blocked request is aborted. If the document request is aborted, the shot is `Private address`. |

Domain entry has a second gate in [app/domain_parser.py](../app/domain_parser.py). Saved hostnames must be DNS names with a public suffix from the bundled list. IP addresses, `localhost`, unknown suffixes, and a bare public suffix are rejected. A pasted URL is reduced to its hostname. Userinfo, port, path, query, and fragment are stripped and mentioned in parse notes. Only `http` and `https` URL schemes are accepted when a scheme is present.

Direct httpx connections use the addresses from that check. The first classified address is the TCP target, and the original hostname stays on the Host header and the TLS server name. A later lookup for the same host in that check reuses those addresses. Direct Chromium captures do the same through a proxy bound to `127.0.0.1` that this process opens for that capture: Chromium connects to the proxy, and the proxy opens the TCP connection to the classified address. The hostname stays the name Chromium uses for Host and TLS. An operator-supplied proxy is different. httpx and Chromium hand the request to that proxy, and the proxy chooses the address it connects to. The name is still refused here when it resolves to a blocked address.

Public IP literals are allowed by `public_hostname_error`. The add-domain form still rejects IP addresses, so a monitor row is always a hostname. A redirect or a crawled link can still be an IP. If that IP is public, the fetch continues.

## Screenshot files

`GET /screenshots/{shot_id}` loads the row by integer id. `shot.path` is a file name under `DATA_DIR/screenshots`. The route resolves that name, and it serves the file only when the resolved path is inside that directory and is a regular file. An older absolute path is served only when it already points inside the current screenshots directory, and that response rewrites the row to the file name. A missing row, a null path, a relative path that is not a bare file name, or a path outside the directory is a 404. The client cannot pass a filesystem path.

## Proxy URLs

`parse_proxy_url` accepts `http` and `https` URLs that have a hostname. `socks`, `socks4`, `socks5`, and `socks5h` are rejected with a message that authenticated SOCKS proxies are not supported. Other schemes are rejected.

The full URL, including userinfo, is stored in `proxies.url`. The settings page prints host and port only (`proxy_label`) and tells the operator that credentials stay in the database. Anyone who can read `monitor.db` can read those URLs. Proxy checks send that URL to httpx and, for screenshots, to Playwright.

httpx clients are constructed with `trust_env=False`, so a proxy is used only when the monitor flag is on and the URL came from this table.

## CSV import

The preview route reads the upload into memory and rejects a body larger than 1_000_000 bytes, a non-UTF-8 body, and more than 500 data rows. Rejected rows are shown and are not written. The import route accepts the JSON list the preview rendered, caps it at 500 items, and runs each item through `parse_domain` again. Imported rows start on Watch for 30 days. A duplicate hostname is skipped.

## Chromium

Playwright launches headless Chromium with `--disable-dev-shm-usage`. When the process is root (`geteuid() == 0`), it also passes `--no-sandbox`. A process running as a normal user does not. Screenshot contexts set `ignore_https_errors=True`, so a certificate error does not stop a capture of a page the HTTP probe was able to classify.

The browser process is local. It loads the monitored page and that page's subresources, subject to the route guard above.

## Data on disk

`DATA_DIR` holds the SQLite database, the WAL files, the secret, and JPEGs of monitored pages. The database is not encrypted. Screenshot retention removes the file and leaves the check row. Deleting a monitor unlinks its files and then deletes the rows.

Registration snapshots store registrar, dates, nameservers, statuses, whether the name is registered, and the hosting company when an IP lookup succeeded. They do not store the raw registry payload. Crawl rows store URL, status, error, and title, not the HTML.

## Operational expectations

A localhost install with the password unset stays an open UI on `127.0.0.1`. The session cookie is accepted on HTTP, and login throttling is not in front of the pages.

When the port is reachable beyond the machine, set `DOMAIN_MONITOR_PASSWORD`. Login attempts are then throttled as described above. Keep `HOST` at `127.0.0.1` unless exposure is intentional. `HOST=0.0.0.0` without that password is an open UI. `./data` must be writable by the user who runs the process.

Set `DOMAIN_MONITOR_HTTPS_ONLY` when TLS ends at this process. Leave it unset when a reverse proxy terminates TLS and forwards plain HTTP. Direct checks connect to the address the safety check allowed. A monitor that uses the operator proxy still lets that proxy open the target connection.

`data/` is gitignored. Do not commit `monitor.db`, `secret.key`, or screenshots.
