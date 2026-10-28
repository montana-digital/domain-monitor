# External services

Every check runs on the machine that hosts Domain Monitor. The process opens outbound connections itself. It does not call a hosted monitoring API, and it does not receive inbound webhooks.

This repository is a self-hosted FastAPI process, run with Docker or with Uvicorn on Debian or Ubuntu. It is not a Netlify site and it is not a Render service. There is no `netlify.toml`, no serverless function entry, and no `render.yaml`.

## What the process contacts

| Dependency | When | Code |
| --- | --- | --- |
| The monitored hostname, `https://` then maybe `http://` | Every check, once per vantage | [app/checks/http_check.py](../app/checks/http_check.py) |
| The same site through an operator proxy | When the monitor's proxy flag is on and the pool has enabled proxies | httpx `proxy=` and Playwright proxy config |
| `robots.txt` and same-site links | When the crawl flag is on | [app/checks/crawl.py](../app/checks/crawl.py) |
| Page subresources during a screenshot | When the screenshot flag is on | [app/checks/screenshot.py](../app/checks/screenshot.py) |
| IANA RDAP bootstrap, then the registry RDAP endpoint | Registration job, at most daily per monitor, and once on add | `whoisit` in [app/checks/registration.py](../app/checks/registration.py) |
| WHOIS on port 43 | When RDAP does not return a usable record | `python-whois` in the same module |

User agents are fixed strings in [app/checks/agents.py](../app/checks/agents.py) (Chrome 131 desktop and mobile). Crawl fetches use the desktop string. The process does not rotate identities.

## Monitored sites

The probe requests `{scheme}://{hostname}/` with an `Accept` header that prefers HTML. Redirects are followed up to 10 hops for the status probe. The crawl follows up to 5 hops itself and stops when the next host is a different registrable domain or a blocked address. Screenshots navigate to the homepage of the winning scheme and load whatever that page requests, aborting a request whose host fails the address check.

Timeouts are 20 seconds for a status request, 10 seconds for a crawl request, and 30 seconds for screenshot navigation. The whole check still ends at 180 seconds.

A transport error, HTTP `429`, or HTTP `503` is retried once on the same scheme after 2 seconds. The app does not implement a backoff schedule beyond that, and it does not contact the site again until the monitor interval elapses or someone clicks Check now.

## Proxies

Proxies are optional and operator-supplied. The URL is saved in SQLite from the Settings form. Allowed schemes are `http` and `https`. SOCKS URLs are rejected.

Only monitors with `use_proxy` enabled send traffic through the pool, and only through rows marked enabled. The pool is shared by every such monitor. Direct vantages still run first and do not use the proxy.

httpx is created with `trust_env=False`. Environment variables such as `HTTP_PROXY` are not a second, hidden proxy configuration.

A proxy can be an address on the operator's own network. The SSRF checks apply to the monitored hostname and to redirect and crawl targets. They are not a filter on the proxy host itself. The proxy URL is the operator's configuration.

## RDAP

`whoisit.bootstrap()` runs the first time a lookup finds the library unbootstrapped. That call fetches the IANA RDAP bootstrap files so later `whoisit.domain(domain)` requests can find the registry for the TLD. The lookup runs in a worker thread.

The domain passed in is the monitor's registrable domain, not a hostname that still has `www`. A successful mapping stores registrar, created, updated, expires, nameservers, and statuses. The raw JSON is discarded. If RDAP returns none of those fields, the code treats it as failure and continues to WHOIS.

A failed lookup is stored and the previous successful snapshot stays on the detail page. The scheduler waits 24 hours from the latest attempt, successful or not, before it tries that monitor again. Adding a domain queues a lookup immediately.

## WHOIS

`whois.whois(domain)` from `python-whois` runs only after RDAP did not produce a successful record. That library speaks WHOIS on port 43 to the server it selects for the domain. The same field mapping is applied. Dates are kept when the library returns a `datetime`. A list uses its first item when that item is a `datetime`.

When both sources fail, the snapshot error contains both messages: `RDAP failed: … WHOIS failed: …`. The banner names each monitor whose latest snapshot failed. A success for one monitor leaves another monitor's failure visible.

RDAP and WHOIS share a 30-second deadline. A lookup that does not return in that time is stored as `Registration lookup timed out.` and the registration slot is released. `resolve_public` in [app/safety.py](../app/safety.py) calls `getaddrinfo` on a worker thread and waits 5 seconds. A resolve that exceeds that wait is `DNS failed`. A blocked call in the worker thread can outlive the wait; the check or registration task does not. A direct httpx or Chromium connection uses the first address from that check. The hostname stays on Host and TLS. An operator proxy still opens its own connection to the target.

## Public Suffix List

Hostname parsing uses `tldextract.TLDExtract(suffix_list_urls=(), fallback_to_snapshot=True)` in [app/domain_parser.py](../app/domain_parser.py). The empty URL tuple means parsing does not download the Public Suffix List. It uses the snapshot packaged with the installed `tldextract`. A suffix that is newer than that snapshot is rejected as `Unknown public suffix` until the dependency is upgraded. Rebuilds that care about new suffixes should bump `tldextract` and reinstall.

## Chromium

Playwright downloads Chromium at image build time (`playwright install --with-deps chromium`) or, on a bare-metal install, when the operator runs that command. `PLAYWRIGHT_BROWSERS_PATH` in the image is `/ms-playwright`. At runtime the browser is a local process. There is no remote browser service, screenshot SaaS, or cloud rendering API.

If the browser is not installed, HTTP checks still run. Launch failure sets the banner `Screenshots are unavailable: …` and the screenshot outcome is `failed`.

## What this app does not call

The process does not send mail, post to chat, push to a status page, or report telemetry. There is no OAuth provider, no payment provider, and no object store. SQLite on the data volume is the database. Fonts, CSS, and HTMX are files under [app/static/](../app/static/). Pages do not load scripts or styles from a CDN.

The UI polls itself (`/fragment/monitors` and `/notices`). Those requests stay on the Domain Monitor origin.
