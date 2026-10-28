# Check pipeline

A check starts when a monitor is first saved, when someone posts Check now, or when the scheduler finds `next_check_at` due. The queue and save path are [app/service.py](../app/service.py). The ordering and deadline are [app/checks/runner.py](../app/checks/runner.py). Address refusal is [app/safety.py](../app/safety.py) and is described with the other controls in [security.md](security.md).

Saving setup on an existing monitor updates the interval and the four flags. [edit_domain](../app/routes/pages.py) moves `next_check_at` only when the new interval is due sooner than the stored time. That candidate is `last_checked_at` plus the new interval, or now when the monitor has never finished a check. A longer interval leaves the stored due time alone. Check now loads the flags when the check runs, so that run uses the values just saved.

## Check level

[app/levels.py](../app/levels.py) derives a label from the four flags. It is not stored.

| Level | When |
| --- | --- |
| Status | Screenshot, crawl, proxy, and mobile are all off. |
| Visual | Screenshot is on, and crawl, proxy, and mobile are off. |
| Full | Crawl, proxy, or mobile is on. Screenshot may be on or off. |

Full does not mean every component ran. It means at least one of those three options is enabled, so the check may use extra vantages or a crawl. The detail page still shows each component's own outcome.

## Vantages

`build_vantages` builds an ordered list and keeps the first six (`VANTAGE_CAP`).

1. `Direct desktop` with no proxy.
2. `Direct mobile` when the mobile flag is on.
3. For each enabled proxy, in proxy id order: `Proxy desktop (host:port)`, then `Proxy mobile (host:port)` when mobile is on.

A monitor with the proxy flag on and no enabled proxies only gets the direct vantages. The detail page shows a banner in that case.

Each vantage is one call to the prober: hostname, profile (`desktop` or `mobile`), and an optional proxy URL. Profiles select the user agent in [app/checks/agents.py](../app/checks/agents.py):

- Desktop: Chrome 131 on Windows 10.
- Mobile: Chrome 131 on Android 14, Pixel 8.

## Deadline

`execute_check` uses a 180-second budget from `time.monotonic`. Each probe is wrapped in `asyncio.wait_for` with the time remaining. When the budget is spent:

- The current probe is recorded as `Check timed out` and later vantages are not started.
- A screenshot that has not started is `failed` with `Check timed out`.
- A crawl that has not started is `failed` with `Check timed out`.
- A crawl that has already started receives the same deadline and can finish as `partial` if it saved at least one page.

The budget is shared. A slow proxy probe can consume the time that screenshot and crawl would have used.

## HTTP probe

[app/checks/http_check.py](../app/checks/http_check.py) `HttpProber.probe` refuses a blocked hostname before any request. It then calls `probe_hostname`.

For a scheme, the client sends one GET. It retries that same scheme once, after a 2-second sleep, when the result is status `429` or `503`, or when the status is null and an error string is set. A normal `4xx` or `5xx` is not retried.

Scheme order:

1. Try `https://{hostname}/`.
2. If that attempt produced a status code, return it. A `404` on HTTPS does not fall through to HTTP.
3. If HTTPS produced no status code, try `http://{hostname}/` the same way.
4. If both produced no status code and the HTTPS error is `TLS failed`, keep the HTTPS error. Otherwise keep the HTTP result.

`send_once` uses httpx with `follow_redirects=True`, at most 10 redirects, a 20-second timeout, `trust_env=False`, and the vantage proxy URL when there is one. `trust_env=False` means `HTTP_PROXY` and similar variables in the process environment are ignored. An event hook calls `public_hostname_error` on every request URL, including redirect targets, and raises `PrivateTarget` for a blocked host.

The recorded result includes status, the scheme that was requested (not the scheme of the final URL), the final URL, latency in milliseconds, and the `Last-Modified` header.

Error strings produced by `classify_http_error`:

| Condition | Stored error |
| --- | --- |
| Blocked address | `Private address` |
| httpx timeout | `Timed out` |
| Too many redirects | `Redirect loop` |
| Proxy error | `Proxy connection failed` |
| Connect error that mentions certificate, TLS, SSL, or handshake | `TLS failed` |
| Connect error that looks like DNS | `DNS failed` |
| Other connect or transport error | `Connection failed` |
| Anything else | `Check failed: unexpected error` |

The unexpected case is logged with a traceback. The page shows the short string.

## Which attempt becomes the status

`_display_status` picks the row shown on the monitor and on the event:

1. The first attempt whose status is `200`–`299`, in vantage order. Its scheme is the event scheme. Its error is cleared.
2. Otherwise the direct desktop attempt's status code, when it has one, even when that code is `4xx` or `5xx`.
3. Otherwise the direct desktop error, or `Check failed.` when that error is empty.
4. If direct desktop never ran, `No check was attempted.`

So a Full check can show green because a proxy or the mobile user agent returned `2xx` while direct desktop failed. The attempt table on the detail page lists every vantage that ran.

Screenshot and crawl use `_capture_target`, which is the same first `2xx`, otherwise direct desktop if it has a status code, otherwise the first attempt that has a status code. The URL is `{scheme}://{hostname}/` using that attempt's scheme and, for a proxy success, that attempt's proxy URL. A target with no scheme skips capture and crawl with `No HTTP response to capture` or `Homepage unreachable`.

## Partial

`partial_reason` returns a reason only when a status code was saved.

| Component state | Reason fragment |
| --- | --- |
| Screenshot outcome `failed` | `Screenshot failed` |
| Crawl outcome `failed` | `Crawl failed` |
| Crawl outcome `partial` | `Crawl incomplete` |

Fragments are joined with a comma. The event is `partial` when the joined string is non-empty. A check that never got an HTTP status is not marked partial, even when screenshot and crawl were skipped or failed because there was nothing to capture.

## Screenshots

[app/checks/screenshot.py](../app/checks/screenshot.py) runs only when the monitor's screenshot flag is on. A process-wide asyncio lock serializes capture, including the browser launch. The check releases its check slot for that call and takes a slot again before the crawl and the save, so probes are not stuck behind the browser lock. A direct capture sends Chromium through a local proxy that connects to the address the safety check allowed. A capture that uses an operator proxy uses that proxy instead.

Viewports:

| Profile | Size |
| --- | --- |
| desktop | 1280 × 800 |
| mobile | 390 × 844, added when the mobile flag is on |

Both shots use the capture-target URL. Mobile is a second viewport of that page, not a separate origin.

Chromium launches headless through Playwright. Arguments are `--disable-dev-shm-usage`, plus `--no-sandbox` when `os.geteuid()` exists and returns 0. The Docker image runs as uid 1000, so the container does not take that path. A disconnected browser is closed and launched again. One browser is reused across checks.

Each shot gets a browser context with the matching user agent, viewport, `ignore_https_errors=True`, and the Playwright proxy config when the capture target used a proxy. Username and password are taken from the proxy URL. Every network request in the page is routed through a guard that aborts the request when the host is blocked. If the aborted request is the document, the shot is stored as `Private address`.

Navigation waits for `domcontentloaded` with a 30-second timeout. The image is a JPEG at quality 60 of the viewport, not the full page. A Playwright timeout becomes `Timed out`. Other failures become `Check failed: unexpected error` unless the exception text contains `timeout`.

Outcomes:

- `succeeded` when every requested shot has image bytes and no error.
- `failed` when the browser cannot start, the deadline is already gone, there is no HTTP target, a shot has an error, or no shots came back.

Launch failures set `notices.browser_error` to a short message (at most 180 characters, or `The browser could not be started.`). A later successful launch clears it.

Bytes are written in `_save_outcome` to `{event_id}-{viewport}.jpg`. The `screenshots.path` value is that file name, relative to `DATA_DIR/screenshots`. An `OSError` on write sets `notices.disk_error` and, when no shot could be stored, forces the screenshot outcome to `failed` with `Could not write the screenshot file.` A later successful write clears the disk banner. The partial flag is recomputed after the write, so a capture that succeeded in memory and failed on disk is still partial.

## Crawl

[app/checks/crawl.py](../app/checks/crawl.py) runs only when the crawl flag is on. It uses the capture-target scheme and proxy, the desktop user agent, a 10-second timeout per request, and `follow_redirects=False` so redirects can be checked one hop at a time.

Limits:

| Limit | Value |
| --- | --- |
| Depth | 0 (homepage), then layer 1, then layer 2 |
| Pages per layer after the homepage | 20 (`PAGE_CAP`) |
| Response body kept | 2_000_000 bytes |
| Redirect hops | 5 |
| URL length | 2000 characters |
| Title | 200 characters |

`robots.txt` is fetched first from the start origin. Rules are the `Disallow` lines in a `User-agent: *` group, matched as a path prefix. A missing or failed robots file adds the note `robots.txt could not be fetched; crawl continued.` and the crawl proceeds. Disallow rules apply to layers 1 and 2. The homepage is always requested. Skipped paths add the note `robots.txt disallows some paths.` and are not counted as failures.

Same-site means `parse_domain` on the link host yields the same registrable domain as the monitor. `bbc.co.uk` includes `www.bbc.co.uk`. Links come from `<a href>`. Fragments, `mailto:`, `javascript:`, and `tel:` are ignored. The stored URL lowercases the scheme and host, keeps the path, and drops the query and fragment.

Redirects to another registrable domain stop with `Left the registrable domain.` Redirects to a blocked address stop with `Private address`. Each request and each redirect target is checked. Bodies with a content type of image, video, audio, PDF, zip, or octet-stream are not parsed for links. HTML and XML are parsed. A body that has no useful content type and starts with `<` is treated as HTML.

Outcomes:

| Outcome | When |
| --- | --- |
| `failed` | The homepage could not be fetched, the deadline was already gone, or the deadline hit before any page was saved. |
| `partial` | At least one page was saved, and a later page failed or the deadline hit mid-crawl. |
| `succeeded` | No page was counted as failed. |

A failed page after the homepage increments `crawl_failed` and the crawl continues. `crawl_saved` counts pages that returned a response without an error.

## Registration

Registration is a separate job, not part of the 180-second check. It is queued when a domain is added and again when the newest snapshot for that monitor is missing or older than 24 hours. The lookup uses `registrable_domain`, so `www.bbc.co.uk` is queried as `bbc.co.uk`.

[app/checks/registration.py](../app/checks/registration.py) tries RDAP, then WHOIS:

1. `whoisit.bootstrap()` runs once per process when the library reports it is not bootstrapped. `whoisit.domain(domain)` is the RDAP call.
2. A successful RDAP record is returned immediately.
3. Otherwise `whois.whois(domain)` runs (port 43 via `python-whois`).
4. If both fail, the stored error is `RDAP failed: {rdap error} WHOIS failed: {whois error}`.

A record counts as success when at least one of registrar, created, updated, expires, nameservers, or statuses is present. Dates are kept only when the library returned a `datetime` (or a list whose first item is a `datetime`).

The snapshot is always inserted, including failures. The newest 30 rows for that monitor are kept. The process banner lists each monitor whose latest snapshot failed, by hostname. A later success for one monitor leaves another monitor's failure on the banner. The detail page shows the newest successful snapshot when the latest attempt failed, and it shows the time and text of that failure.

The lookup runs in a worker thread under the registration semaphore of 2, and the task is in the set shutdown waits on. The wait is 30 seconds. A timeout is stored as a failed snapshot whose error is `Registration lookup timed out.`, and the slot is released. An unexpected exception is stored as `Check failed: unexpected error`. A blocked worker thread is not killed; the asyncio task stops waiting for it.

## Saving the check

`_save_outcome` opens a session and writes, in one transaction:

- The `check_events` row, with `next_check_at` on the monitor set to the finish time plus `interval_minutes`.
- One `check_attempts` row per attempt.
- Proxy `last_error` updates for proxy attempts.
- One `crawl_pages` row per crawled page.
- JPEG files, then `screenshots` rows. If the commit hits a SQLite busy error, the session rolls back, those new files are deleted, the code sleeps 0.2 seconds, and it tries once more. A second busy error is logged. The full attempt list is not written again. One event is saved with the measured status code and the error `Result could not be stored.` (appended to the measured error when there was one), and `next_check_at` moves forward by the interval.

`_save_crash` writes an event with `Check failed: unexpected error` and still moves `next_check_at` forward, so a crashing monitor does not stay permanently due.

The log line after a normal save is `Checked {hostname} status={code or error} partial={bool}`.

## Worked sequence

A Full monitor with screenshot, crawl, mobile, and one proxy, all inside the deadline:

1. Probe direct desktop, then direct mobile, then proxy desktop, then proxy mobile.
2. Choose the first `2xx` as the status, scheme, and capture target.
3. Under the browser lock, capture desktop and mobile JPEGs of `{scheme}://{hostname}/` through that target's proxy.
4. Crawl the homepage and up to two further layers on the registrable domain, through that same proxy, until the deadline.
5. Write the event, attempts, pages, and files. Set the monitor's last status and the next check time.
