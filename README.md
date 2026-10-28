# Domain Monitor

Domain Monitor is a single-process Python app for watching web domains from a Linux machine. It records the HTTP status, and can also take a browser screenshot, crawl two layers of the same site, and retry through a proxy or a mobile user agent.

Checks run from the machine where the app is installed. A green status is a final `2xx` response after redirects.

## Design record

The documents in [docs/README.md](docs/README.md) describe the architecture, data model, check pipeline, security, external services, operations, and UI. [AGENTS.md](AGENTS.md) points a later change at the right document.

## Run on Ubuntu

From the repository directory, with Python 3.12 or newer:

```bash
python3 launch.py
```

The first run creates `.venv`, installs this checkout, and downloads Chromium into `.playwright`. It may ask for a sudo password to install `python3-venv` and the libraries Chromium needs. Later runs start the server.

Open `http://127.0.0.1:8000`. The database and screenshots are stored in `./data`.

Set a password before you expose the port beyond localhost:

```bash
DOMAIN_MONITOR_PASSWORD=choose-a-long-password python3 launch.py
```

`HOST` defaults to `127.0.0.1`. Set `HOST=0.0.0.0` only when other machines must reach the port, and keep the password set. `python3 launch.py --repair` repeats the package and Chromium install.

To keep the process up after logout:

```bash
python3 launch.py --service
```

That writes a systemd unit which runs `.venv/bin/python -m app.main` as the user who installed it. Data stays in `./data`, and Chromium stays in `.playwright`. Starting the service does not install packages again.

## Run with Docker

Docker is optional. The image includes Chromium, which screenshots need.

```bash
docker compose up --build -d
```

Open `http://127.0.0.1:8000`. The database and screenshots are stored in `./data`.

Set a password before you expose the port beyond localhost:

```bash
# in the environment of the compose service
DOMAIN_MONITOR_PASSWORD=choose-a-long-password
```

The container runs as uid 1000. `./data` must be writable by that user. Chromium keeps its sandbox. Set `DOMAIN_MONITOR_HTTPS_ONLY=1` only when TLS ends at this process.

## Local development

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On Linux or macOS, activate the virtualenv with `source .venv/bin/activate`. Screenshots also need `playwright install chromium`. Until that browser is installed, HTTP checks still run and the page shows that screenshots are unavailable.

## What you can configure

- Poll interval: 1, 5, 15, 30, or 60 minutes. The first check is queued as soon as the domain is saved.
- Screenshot: one desktop viewport. A mobile viewport is added when the mobile user agent is on.
- Crawl: the homepage, then up to 20 same-site links, then up to 20 links found on that first layer. Same-site means the same registrable domain (`bbc.co.uk` includes `www.bbc.co.uk`).
- Proxy pool: HTTP or HTTPS URLs saved in Settings. A monitor uses them only when its proxy option is on. Authenticated SOCKS proxies are rejected.
- Mobile user agent: a second request with a phone browser identity.

Check level is derived from those options:

- **Status** — HTTP only.
- **Visual** — screenshot, with crawl, proxy, and mobile off.
- **Full** — crawl, proxy, or mobile is on.

On a Full check, any `2xx` across the direct request, the mobile user agent, and the proxy pool becomes the status. The detail page lists every attempt. The list is capped at six attempts.

## Adding domains

Use the form, or a UTF-8 CSV of up to 500 rows and 1 MB. A header is optional:

```csv
domain,interval_minutes,screenshot,crawl,use_proxy,mobile_ua
google.com,5,no,no,no,no
www.bbc.co.uk,15,yes,yes,no,yes
```

A file with one hostname per line is also accepted. Missing columns mean a 5 minute interval and every extra check off. The preview shows rejected rows and does not save them. A hostname is accepted only when it has a real public suffix, so `bbc.co.uk` passes and `example.notavalidsuffix` does not. Pasted URLs are reduced to the hostname. IP addresses are rejected.

## When something fails

Failures stay on the page. There is no email or chat alert.

- The status cell shows **Pending** before the first result, the response code, or a short error such as **Timed out** or **DNS failed**.
- **Partial** means the HTTP result was saved and the screenshot or crawl did not finish.
- **Overdue** means the last completed check is older than the interval plus one minute.
- **Recent problems** lists the latest component failures.
- A banner appears when the browser cannot start, a screenshot cannot be written, registration lookups are failing, or the scheduler has not run for a minute.
- Registration uses RDAP first and port 43 WHOIS when RDAP cannot route the suffix. A failed lookup keeps the last good record and shows the time and reason.
- HTTPS is tried first. If it never connects, the check tries HTTP once. A transport error, `429`, or `503` is retried once.
- One domain cannot stop the others. A check stops after three minutes and saves whatever finished.
- Private, loopback, and link-local addresses are not requested.

Screenshot files older than the retention setting (7, 30, or 90 days) are deleted. The check row remains and says the image was removed.

## Environment

| Variable | Purpose |
| --- | --- |
| `DATA_DIR` | SQLite database, secret, and screenshots. Defaults to `./data`. |
| `HOST` | Bind address. Defaults to `127.0.0.1`. |
| `PORT` | Bind port. Defaults to `8000`. |
| `DOMAIN_MONITOR_PASSWORD` | One shared password. Leave unset only on localhost. |
| `DOMAIN_MONITOR_SECRET` | Session signing key. A key is created in the data directory when this is unset. |

## Versioning

The version in `pyproject.toml` starts at `0.1.0`. The first commit keeps that version and is tagged `v0.1.0`. Each later commit raises the patch number and adds a matching `vX.Y.Z` tag.

After a clone, point Git at the tracked hooks. Git does not copy that setting:

```bash
git config core.hooksPath .githooks
```

To amend a commit without raising the version, set `SKIP_VERSION_BUMP=1` for that command. In PowerShell:

```powershell
$env:SKIP_VERSION_BUMP = "1"
git commit --amend
```
