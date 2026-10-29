# Operations

The install is [launch.py](../launch.py): `python3 launch.py` from the repository, or `python launch.py` on Windows. When `.venv` is missing it asks whether to install the full feature set or the same app without the browser. When that virtualenv is already current and the core packages match this checkout, it starts the process. A missing browser leaves screenshots off and does not block the start. A failed registration, Playwright, Chromium, or library install leaves that feature off and still starts the core app. The process is Uvicorn serving `app.main:app`, with SQLite on that machine. Chromium is local when the full install has it. Docker is not used. The operator steps are in [README.md](../README.md).

## Environment

[app/config.py](../app/config.py) reads these variables once, at process start. A change requires a restart. Retention and the proxy pool are the exception: they live in SQLite and are read when used.

| Variable | Default | Role |
| --- | --- | --- |
| `DATA_DIR` | `./data`, resolved to an absolute path | SQLite database, `secret.key`, and screenshots. The directory is created if it is missing. |
| `HOST` | `127.0.0.1` | Bind address used by `python -m app.main`. |
| `PORT` | `8000` | Bind port used by `python -m app.main`. |
| `DOMAIN_MONITOR_PASSWORD` | unset | One shared password. Unset means the UI does not ask for a login. |
| `DOMAIN_MONITOR_SECRET` | a key created in `DATA_DIR/secret.key` | Session signing key. |

`python -m app.main`, which is what the launcher starts, reads `HOST` and `PORT`. Leave `HOST` at `127.0.0.1` unless other machines must reach the port. Set the password before the port is reachable beyond the machine. The launcher prints a warning when `HOST` is not a loopback address and `DOMAIN_MONITOR_PASSWORD` is unset, then still starts.

`start_scheduler` is not an environment variable. It defaults to true. Tests construct `Settings` with it false.

## Data directory

| Path | Contents |
| --- | --- |
| `monitor.db` | SQLite database. WAL mode also creates `monitor.db-wal` and `monitor.db-shm`. |
| `secret.key` | Session secret, mode `0600` when the chmod succeeds. |
| `screenshots/{event_id}-{viewport}.jpg` | Viewport JPEGs. `viewport` is `desktop` or `mobile`. |

`data/` is listed in [.gitignore](../.gitignore), together with `.env` and `*.db`. Do not commit the database, the secret, or screenshots.

Copy the whole directory to move a deployment. Screenshot rows store the JPEG file name. The route resolves that name against the current `DATA_DIR/screenshots`, so the database and the screenshot directory can move together. An older absolute path is served only when it already points at a file inside the current screenshots directory, and that read rewrites the row to the file name.

The database file is not encrypted. Proxy URLs, including credentials, are in `proxies.url`.

## Health

`GET /health` returns status 200 and `{"status": "ok"}` when the database answers `SELECT 1` and the scheduler is not stalled. A database that cannot be opened, or an enabled scheduler with no tick for more than 60 seconds, returns status 503.

## Ubuntu launcher

[launch.py](../launch.py) uses only the standard library until `.venv` exists. It changes to the repository directory, so the working directory does not matter. Linux and Windows both use it. The virtualenv interpreter is `.venv/bin/python` on Linux and `.venv/Scripts/python.exe` on Windows. `--yes` accepts the full install, including Chromium, without a prompt. `--without-browser` installs registration lookups and skips Chromium, also without a prompt. A terminal that cannot answer, and none of `--yes`, `--without-browser`, or `--repair`, exits 2 and changes nothing. `--service` is Linux-only.

Each step is skipped when it is already true:

- Python is 3.12 or newer. The script does not replace the system Python.
- `venv` and `ensurepip` import. If they do not, it installs the venv package for the running interpreter with `sudo apt-get install -y` (`python3.13-venv` on Ubuntu 25). That install may ask for the desktop sudo password. Without sudo it prints the command and stops. Chromium's library install may ask for the same password. Without sudo, or if that install fails, screenshots stay off and the server still starts. If Ubuntu Software holds `/var/lib/dpkg/lock-frontend`, the launcher waits and tries again. Chromium's library install sets `DEBIAN_FRONTEND` inside the root shell.
- `.venv` exists and its Python is 3.12 or newer. A missing or older virtualenv is created only after the operator chooses an install, or passes `--yes`, `--without-browser`, or `--repair`. Enter, or any answer other than `1` or `2`, leaves the directory in place. The same interpreter that started the script creates it. A current virtualenv is not deleted when a later install step fails.
- The virtualenv imports FastAPI and Uvicorn, and the installed `domain-monitor` version matches `pyproject.toml`. That is the core app: `pip install -e .`. When the chosen install includes registration, `pip install -e ".[registration]"` adds whoisit and python-whois. When it includes Chromium, a separate `pip install` adds the Playwright requirement from the `full` extra. Those two installs are optional. A failure leaves that feature off and the server still starts. A version mismatch reinstalls core, then any extra this virtualenv already has, so a core reinstall does not drop Playwright or whoisit. An editable install serves templates from the checkout, so a later pull of Python files is used on the next start. If the core install itself fails, the server does not start.
- Chromium's `chrome` binary is under `.playwright/chromium-*`, at `chrome-linux64/chrome`, `chrome-linux/chrome`, or `chrome-win/chrome.exe`. The browser download is large and may take a while. `PLAYWRIGHT_BROWSERS_PATH` is `.playwright`. `playwright install chromium` runs when the operator chooses the full install or passes `--repair`. That command does not install WebKit, Firefox, or a container tool. An ordinary launch of a current virtualenv does not download it. If Playwright imports and the Chrome binary or `.playwright/.chromium-ok` is missing, the process starts with screenshots off. A connection reset during a requested download is retried. The stamp is written only after the download exits 0. An idle socket is allowed 10 minutes without data; a transfer that is still moving is left alone. If that download fails, screenshots stay off and the server still starts. `--repair` retries the download.
- `.playwright/.system-deps` exists on Linux when Chromium's libraries installed. Before `playwright install-deps chromium`, the launcher runs that command with `--dry-run` and skips the install if the output names Docker, containerd, podman, or WSL. The real install runs as root during a full install or `--repair`, and the stamp is written only after it exits 0. Windows skips this step. An ordinary launch does not call apt. A refused sudo password, a missing `apt-get`, or a failed install leaves screenshots off and the server still starts. `--repair` tries the libraries again.

`python3 launch.py --repair` repeats the pip install, the browser download, and the library install. If an optional step fails, the core app still starts.

The script then sets `PLAYWRIGHT_BROWSERS_PATH` to `.playwright` and replaces itself with the virtualenv Python running `-m app.main`. On Linux that interpreter is `.venv/bin/python`. On Windows it is `.venv/Scripts/python.exe`. `DATA_DIR`, `HOST`, `PORT`, `DOMAIN_MONITOR_PASSWORD`, and `DOMAIN_MONITOR_SECRET` pass through. Defaults stay `./data`, `127.0.0.1`, and port 8000.

`python3 launch.py --service` does the same install, then writes `/etc/systemd/system/domain-monitor.service` and enables it. `ExecStart` is `.venv/bin/python -m app.main`, not `launch.py`, so boot does not call apt. `WorkingDirectory` is the repository. `PLAYWRIGHT_BROWSERS_PATH` is `.playwright`. `DATA_DIR` is the repository `data` directory unless `DATA_DIR` was already set. The service user is the account that ran the installer (`SUDO_USER` when the script itself was started with sudo). The unit is `Type=simple`, `After=network-online.target`, `Wants=network-online.target`, `Restart=on-failure`, and `RestartSec=5`. It is mode `0600` when it stores a password or a session secret, and `0644` otherwise. A service user of root still makes Chromium add `--no-sandbox`.

`.playwright/` is listed in [.gitignore](../.gitignore). Copy the repository and `.playwright` together if the browser should move with the checkout. Copy `data/` with them when the database and screenshots should move too.

When Chromium or Playwright is missing, HTTP checks still run and screenshots stay off. The Screenshot checkbox is disabled and the saved flag is left as it is. A browser that is present and later fails to launch still sets the banner `Screenshots are unavailable: …`, and that screenshot outcome is `failed`.

### Terminal

On an interactive terminal the launcher prints a block-letter wordmark for Domain Monitor, then a progress panel. The panel lists the platform, Python, the virtualenv, the package install, Chromium, system libraries, the listen address, and either the server start or the systemd unit. Virtualenv, packages, Chromium, and system libraries are marked skipped when they are already in place. The version in the wordmark comes from `pyproject.toml`.

`apt`, `pip`, and the browser download still print under the panel, so a sudo password prompt stays on the terminal. After that output the panel is printed again. The panel is not an alternate screen. Before the process is replaced by Uvicorn, the last panel remains in the scrollback with the listen URL. `--service` stops on the line that says the service is running.

`NO_COLOR`, or output that is not a terminal, prints the same facts as plain lines without color or cursor movement. The install question is asked only on a terminal. Without one, pass `--yes` or `--without-browser`.

## Local development

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On Linux or macOS the virtualenv is activated with `source .venv/bin/activate`. The editable install serves templates and static files from the source tree. `pytest` uses `pythonpath = ["."]` and `asyncio_default_fixture_loop_scope = "function"` from [pyproject.toml](../pyproject.toml). Dev dependencies are `pytest`, `pytest-asyncio`, and the registration and browser packages, so the current tests install those extras.

Screenshots in development need `playwright install chromium`.

## Dependencies

Core libraries, from [pyproject.toml](../pyproject.toml): FastAPI, Uvicorn, Jinja2, python-multipart, SQLAlchemy, httpx, BeautifulSoup, tldextract, APScheduler, itsdangerous. The `registration` extra adds whoisit and python-whois. The `full` extra adds those and Playwright. The project requires Python `>=3.12`. The package name is `domain-monitor`. The version in the `[project]` table is what the git hooks read.

Declared package data is `templates/*.html`, `static/*`, and `static/fonts/*`. A non-editable install (`pip install .`) includes the three `.woff2` files and their license texts. An editable install serves the same files from the source tree.

## Failure banners

[app/notices.py](../app/notices.py) keeps process-local messages. They reset on restart. The base template loads them on each page and HTMX replaces them from `GET /notices` every 30 seconds.

| Banner | Cause |
| --- | --- |
| `Registration lookups are unavailable.` | The startup RDAP probe did not connect, or whoisit and whois are not installed. Cleared on a later start that connects. |
| `Screenshots are unavailable: …` | Chromium was present and then failed to launch. Cleared after a later launch succeeds. A missing browser disables the Screenshot checkbox instead of using this banner. |
| `Screenshot storage failed: …` | A JPEG could not be written. Cleared after a later write succeeds. |
| `Registration lookup failed for {hostname}: …` | That monitor's latest registration snapshot failed. A later success for a different monitor leaves this line in place. The line goes away when this monitor's own latest snapshot succeeds. |
| `Scheduler has not run in over a minute.` | The scheduler is enabled, and either no tick has been recorded within 60 seconds of start, or the last tick is older than 60 seconds. |

Per-domain states are stored on the monitor and are separate from these banners. The list shows **Pending** before the first result, the status code or a short error, **Partial** when the HTTP result was saved and screenshot or crawl did not finish, and **Overdue** when the last completed check is older than the interval plus one minute while no check is running. **Debug** is a separate page. It lists a domain only when the latest check has no status code, a final code of 300–399 (warning), or any other non-2xx code (serious). A failed screenshot, a failed crawl, or a partial crawl stays on the Partial tag.

`DOMAIN_MONITOR_SEED` defaults to on. The first start inserts a fixed set of about 50 public hostnames that are not already saved, then sets `app_settings.sample_seeded` so a later start does not put deleted rows back. Set `DOMAIN_MONITOR_SEED=0` to skip that insert. Those rows are status checks only.

A single check stops after 180 seconds. One monitor's exception is logged and does not stop the others. A crashed check still moves `next_check_at` forward by the interval.

SQLite `OperationalError` on the check write is retried once after 0.2 seconds. If it fails again, one event keeps the measured status and says `Result could not be stored.`, and the next check moves forward by the interval. The busy timeout on each connection is 5 seconds.

## Versioning

The version starts in the `[project]` table of [pyproject.toml](../pyproject.toml). Git does not copy `core.hooksPath`, so after a clone:

```bash
git config core.hooksPath .githooks
```

[.githooks/pre-commit](../.githooks/pre-commit) exits without a bump when there is no `HEAD` yet, so the first commit keeps the version already in the file. Later commits run [scripts/version_bump.py](../scripts/version_bump.py) `--bump`, which increments the patch number and stages `pyproject.toml`. [.githooks/post-commit](../.githooks/post-commit) reads the version and creates an annotated tag `vX.Y.Z` when that tag does not already exist.

The hooks are shell scripts. [.gitattributes](../.gitattributes) forces LF line endings for `.githooks/*` and `scripts/version_bump.py`.

To amend without raising the version, set `SKIP_VERSION_BUMP=1` for that commit. In PowerShell:

```powershell
$env:SKIP_VERSION_BUMP = "1"
git commit --amend
```

The hook looks for `py`, then `python3`, then `python`.
