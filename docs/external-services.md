# External services

Every check runs on the machine that hosts Domain Monitor. The process opens outbound connections itself. It does not call a hosted monitoring API, and it does not receive inbound webhooks.

This repository is a self-hosted FastAPI process, run with Uvicorn on Linux or Windows through [launch.py](../launch.py). It does not use Docker. It is not a Netlify site and it is not a Render service. There is no `netlify.toml`, no serverless function entry, and no `render.yaml`.

## What the process contacts

| Dependency | When | Code |
| --- | --- | --- |
| The monitored hostname, `https://` then maybe `http://` | Every check, once per vantage | [app/checks/http_check.py](../app/checks/http_check.py) |
| The same site through an operator proxy | When the monitor's proxy flag is on and the pool has enabled proxies | httpx `proxy=` and Playwright proxy config |
| `robots.txt` and same-site links | When the crawl flag is on | [app/checks/crawl.py](../app/checks/crawl.py) |
| Page subresources during a screenshot | When the screenshot flag is on | [app/checks/screenshot.py](../app/checks/screenshot.py) |
| IANA RDAP bootstrap, then the registry RDAP endpoint | Registration job, at most daily per monitor, and once on add | `whoisit` in [app/checks/registration.py](../app/checks/registration.py) |
| WHOIS on port 43 | When RDAP does not return a usable record | `python-whois` in the same module |
| `https://1.1.1.1/cdn-cgi/trace` and `https://[2606:4700:4700::1111]/cdn-cgi/trace` | About page network check, at most every five minutes unless Check again is used | [app/environment.py](../app/environment.py) |
| RDAP for the public address that trace returned | After a successful About trace, when registration is on | `whoisit.ip` through `ip_org_lookup` |

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

The domain passed in is the monitor's registrable domain, not a hostname that still has `www`. A successful mapping stores registrar, created, updated, expires, nameservers, and statuses, and marks the domain registered. The raw JSON is discarded. If RDAP returns none of those fields, the code treats it as failure and continues to WHOIS. A finished lookup that says the name is not registered (RDAP `ResourceDoesNotExist`, or a WHOIS "no match" / "not found") stores Registered = No. A timeout or network error stays unknown and does not wipe the last good record.

A failed lookup is stored and the previous successful snapshot stays on the detail page. The scheduler waits 24 hours from the latest attempt, successful or not, before it tries that monitor again. Adding a domain queues a lookup immediately.

## WHOIS

`whois.whois(domain)` from `python-whois` runs only after RDAP did not produce a successful record. That library speaks WHOIS on port 43 to the server it selects for the domain. The same field mapping is applied. Dates are kept when the library returns a `datetime`. A list uses its first item when that item is a `datetime`.

When both sources fail, the snapshot error contains both messages: `RDAP failed: … WHOIS failed: …`. The banner names each monitor whose latest snapshot failed. A success for one monitor leaves another monitor's failure visible.

RDAP and WHOIS share a 30-second deadline. A lookup that does not return in that time is stored as `Registration lookup timed out.` and the registration slot is released. After the domain lookup, the same job asks `whoisit.ip` for the organization that owns the public address saved by the last check. That call has its own 10-second wait. A private or missing address is skipped. A failed IP lookup keeps the previous host name. The name stored is the registrant organization, not the nameserver. The registrar name is the RDAP entity with the registrar role when the top-level registrar field is empty. The check's address pin is the source of that IP, and the IP lookup talks to the registry, not to the address itself. `resolve_public` in [app/safety.py](../app/safety.py) calls `getaddrinfo` on a worker thread and waits 5 seconds. A resolve that exceeds that wait is `DNS failed`. A blocked call in the worker thread can outlive the wait; the check or registration task does not. A direct httpx or Chromium connection uses the first address from that check. The hostname stays on Host and TLS. An operator proxy still opens its own connection to the target.

## Public Suffix List

Hostname parsing uses `tldextract.TLDExtract(suffix_list_urls=(), fallback_to_snapshot=True)` in [app/domain_parser.py](../app/domain_parser.py). The empty URL tuple means parsing does not download the Public Suffix List. It uses the snapshot packaged with the installed `tldextract`. A suffix that is newer than that snapshot is rejected as `Unknown public suffix` until the dependency is upgraded. Rebuilds that care about new suffixes should bump `tldextract` and reinstall.

## Chromium

Playwright downloads Chromium into `.playwright` when the operator chooses the full install or passes `--repair`. The command is `playwright install chromium`. On Linux the same install then runs `playwright install-deps chromium` for Chromium's shared libraries, after a dry run. That step is skipped when the package list names Docker, containerd, podman, or WSL. Windows does not install system libraries. `PLAYWRIGHT_BROWSERS_PATH` is `.playwright`. At runtime the browser is a local process. There is no container image, remote browser service, screenshot SaaS, or cloud rendering API.

If Playwright or the Chromium binary is missing, or the download or library install fails, HTTP checks still run and the screenshot outcome is `skipped`. Launch failure of a browser that is present sets the banner `Screenshots are unavailable: …` and the screenshot outcome is `failed`.

## About page

Opening About, or the network fragment, checks how this machine reaches the internet. The check is direct: `trust_env=False`, no proxy pool, redirects off, and a five-second timeout per family. IPv4 uses `1.1.1.1`. IPv6 uses `2606:4700:4700::1111`. Both addresses are refused by `is_blocked_ip` before the connection if they ever stop being public. The user agent is `DomainMonitor/{version} (environment check)`, not the Chrome strings used on monitored sites.

A family counts as online only when the trace body includes a public `ip=`. `http=` and `tls=` are shown as the HTTP and TLS versions. A redirect is a failed check and is not followed. A private or otherwise blocked address in the body is discarded and is not sent to RDAP. When registration is on, `ip_org_lookup` runs in a worker thread with the same 10-second wait as a monitor's host lookup. A reverse name uses `getnameinfo` with a 5-second wait. The snapshot stays in memory for five minutes and is not written to SQLite.

## What this app does not call

The process does not send mail, post to chat, push to a status page, or report telemetry. There is no OAuth provider, no payment provider, and no object store. SQLite on the data volume is the database. Fonts, CSS, and HTMX are files under [app/static/](../app/static/). Pages do not load scripts or styles from a CDN.

The UI polls itself (`/fragment/monitors` and `/notices`). The About network card loads `/fragment/about-network` once, and again when Check again is used. Those requests stay on the Domain Monitor origin. The trace request does not.
