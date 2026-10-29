# Domain Monitor

Domain Monitor is a single-process Python app for watching web domains from a Linux or Windows machine. It records the HTTP status, and can also take a browser screenshot, crawl two layers of the same site, and retry through a proxy or a mobile user agent.

Checks run from the machine where the app is installed. A green status is a final `2xx` response after redirects.

## Design record

The documents in [docs/README.md](docs/README.md) describe the architecture, data model, check pipeline, security, external services, operations, and UI. [AGENTS.md](AGENTS.md) points a later change at the right document.

## Run

From the repository directory, with Python 3.12 or newer:

```bash
python3 launch.py
```

On Windows, use `python launch.py`. The launcher uses the repository `.venv`. If that virtualenv is missing, or its Python is older than 3.12, it asks which install to run. Choose `1` for the full install, including Chromium, or `2` for registration lookups without the browser. Enter cancels and changes nothing. `--yes` runs the full install without a prompt. `--without-browser` runs the smaller install without a prompt. A virtualenv whose core packages match this checkout starts the server. That launch does not download Chromium. Screenshots stay off until Chromium is present. `--repair`, or choosing the full install, downloads Chromium.

The terminal prints a Domain Monitor wordmark and a progress panel. On Linux it may ask for a sudo password to install the Python venv package for this interpreter (`python3.13-venv` on Ubuntu 25) and the libraries Chromium needs. The library install is skipped if it would name Docker, containerd, podman, or WSL. A failed core install leaves what finished in place and does not start the server. Run the launcher again to continue that install. If registration, Playwright, the Chromium download, or Chromium's libraries fail, that feature stays off and the server still starts. `python launch.py --repair` retries those optional steps. Declining the install changes nothing.

Open `http://127.0.0.1:8000`. The database and screenshots are stored in `./data`.

Set a password before you expose the port beyond localhost:

```bash
DOMAIN_MONITOR_PASSWORD=choose-a-long-password python3 launch.py
```

`HOST` defaults to `127.0.0.1`. Set `HOST=0.0.0.0` only when other machines must reach the port, and keep the password set. `python launch.py --repair` repeats the package install and downloads Chromium. `python launch.py --without-browser` installs registration lookups and leaves screenshots off.

On Linux, to keep the process up after logout:

```bash
python3 launch.py --service
```

That writes a systemd unit which runs `.venv/bin/python -m app.main` as the user who installed it. Data stays in `./data`, and Chromium stays in `.playwright`. The unit does not run `launch.py`, so boot does not install packages again. `--service` is not available on Windows.

Chromium comes from Playwright into `.playwright`. Nothing in this install uses Docker. The Linux library install is Chromium only, and it is skipped if the package list names Docker, containerd, podman, or WSL.

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

- Watch: checked at the interval you choose (5, 15, 30, 60, or 240 minutes; 30 by default) for the number of days you set (default 30). A change in HTTP status or registration extends that window and keeps the interval. The first check is queued as soon as the domain is saved.
- Routine: when the watch window ends with no change, checks move to every 4 hours, then every 24 hours, then weekly. A later change restarts that clock at 4 hours and does not return the domain to Watch. Check now stays available.
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
google.com,30,no,no,no,no
www.bbc.co.uk,15,yes,yes,no,yes
```

A file with one hostname per line is also accepted. Imported domains start on Watch for 30 days. A blank or missing interval column means 30 minutes. Any other interval must be 5, 15, 30, 60, or 240 minutes. Missing check columns leave every extra check off. The preview shows rejected rows and does not save them. A hostname is accepted only when it has a real public suffix, so `bbc.co.uk` passes and `example.notavalidsuffix` does not. Pasted URLs are reduced to the hostname. IP addresses are rejected.

## When something fails

Failures stay on the page. There is no email or chat alert.

- The status cell shows **Pending** before the first result, the response code, or a short error such as **Timed out** or **DNS failed**.
- **Partial** means the HTTP result was saved and the screenshot or crawl did not finish.
- **Overdue** means the last completed check is older than the interval plus one minute.
- **Debug** lists domains whose latest check is a serious failure or a warning. A partial screenshot or crawl stays on the Partial tag and is not listed there.
- The monitors table shows whether the domain is registered, the registrar, and the company that owns the site IP. Nameservers stay on the detail page.
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
