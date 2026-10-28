# Operations

The usual install on Debian or Ubuntu is [launch.py](../launch.py): `python3 launch.py` from the repository. It creates a virtualenv, installs this checkout, and downloads Chromium into `.playwright`. Docker is an optional image of the same process. Both run Uvicorn serving `app.main:app`, with SQLite and Chromium on that machine. The operator steps are in [README.md](../README.md).

## Environment

[app/config.py](../app/config.py) reads these variables once, at process start. A change requires a restart. Retention and the proxy pool are the exception: they live in SQLite and are read when used.

| Variable | Default | Role |
| --- | --- | --- |
| `DATA_DIR` | `./data`, resolved to an absolute path | SQLite database, `secret.key`, and screenshots. The directory is created if it is missing. |
| `HOST` | `127.0.0.1` | Bind address used by `python -m app.main`. |
| `PORT` | `8000` | Bind port used by `python -m app.main`. |
| `DOMAIN_MONITOR_PASSWORD` | unset | One shared password. Unset means the UI does not ask for a login. |
| `DOMAIN_MONITOR_SECRET` | a key created in `DATA_DIR/secret.key` | Session signing key. |

Docker Compose sets `DATA_DIR=/data`, `HOST=0.0.0.0`, and `PORT=8000`. The image `CMD` does not read `HOST`. It runs:

```sh
uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
```

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

## Docker

[Dockerfile](../Dockerfile) uses `python:3.12-bookworm`. It sets `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, and `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`, installs the package, and runs `playwright install --with-deps chromium`. The image exposes port 8000 and declares a volume at `/data`. The process runs as uid 1000, user `monitor`, so Chromium does not add `--no-sandbox`. The bind-mounted data directory must be writable by that user. A bare-metal process that still runs as root does add `--no-sandbox`.

[docker-compose.yml](../docker-compose.yml) builds that image, publishes `8000:8000`, mounts `./data` on `/data`, and restarts unless stopped. `DOMAIN_MONITOR_PASSWORD` is commented out. A published port with no password is an open UI.

```bash
docker compose up --build -d
```

The UI is at `http://127.0.0.1:8000` when that publish is left as written.

There is no Docker `HEALTHCHECK`. `GET /health` can be used as one. It returns status 200 and `{"status": "ok"}` when the database answers `SELECT 1` and the scheduler is not stalled. A database that cannot be opened, or an enabled scheduler with no tick for more than 60 seconds, returns status 503.

## Ubuntu launcher

[launch.py](../launch.py) uses only the standard library until `.venv` exists. It changes to the repository directory, so the working directory does not matter. On Windows it prints the local development commands and exits.

Each step is skipped when it is already true:

- Python is 3.12 or newer. The script does not replace the system Python.
- `venv` and `ensurepip` import. If they do not, it installs `python3-venv` with apt. That install, and Chromium's libraries, may ask for sudo. Without sudo it prints the command and stops.
- `.venv` exists and its Python is 3.12 or newer. A missing or older virtualenv is created with the same interpreter that started the script.
- The virtualenv imports FastAPI, Uvicorn, and Playwright, and the installed `domain-monitor` version matches `pyproject.toml`. Otherwise it runs `pip install -e .`. An editable install serves templates from the checkout, so a later pull of Python files is used on the next start. A pull that changes the version runs pip again.
- Chromium's `chrome` binary is under `.playwright/chromium-*`, at `chrome-linux64/chrome` or `chrome-linux/chrome`. If it is missing, `PLAYWRIGHT_BROWSERS_PATH` is set to `.playwright` and `playwright install chromium` runs as the installing user.
- `.playwright/.system-deps` exists. If it does not, `playwright install-deps chromium` runs as root and the stamp is written afterwards. The next launch does not call apt.

`python3 launch.py --repair` repeats the pip install, the browser download, and the library install.

The script then sets `PLAYWRIGHT_BROWSERS_PATH` to `.playwright` and replaces itself with `.venv/bin/python -m app.main`. `DATA_DIR`, `HOST`, `PORT`, `DOMAIN_MONITOR_PASSWORD`, and `DOMAIN_MONITOR_SECRET` pass through. Defaults stay `./data`, `127.0.0.1`, and port 8000.

`python3 launch.py --service` does the same install, then writes `/etc/systemd/system/domain-monitor.service` and enables it. `ExecStart` is `.venv/bin/python -m app.main`, not `launch.py`, so boot does not call apt. `WorkingDirectory` is the repository. `PLAYWRIGHT_BROWSERS_PATH` is `.playwright`. `DATA_DIR` is the repository `data` directory unless `DATA_DIR` was already set. The service user is the account that ran the installer (`SUDO_USER` when the script itself was started with sudo). The unit is `Type=simple`, `After=network-online.target`, `Wants=network-online.target`, `Restart=on-failure`, and `RestartSec=5`. It is mode `0600` when it stores a password or a session secret, and `0644` otherwise. A service user of root still makes Chromium add `--no-sandbox`.

`.playwright/` is listed in [.gitignore](../.gitignore). Copy the repository and `.playwright` together if the browser should move with the checkout. Copy `data/` with them when the database and screenshots should move too.

Until Chromium can launch, HTTP checks still run and the UI banner says screenshots are unavailable.

## Local development

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On Linux or macOS the virtualenv is activated with `source .venv/bin/activate`. The editable install serves templates and static files from the source tree. `pytest` uses `pythonpath = ["."]` and `asyncio_default_fixture_loop_scope = "function"` from [pyproject.toml](../pyproject.toml). Dev dependencies are `pytest` and `pytest-asyncio`.

Screenshots in development need `playwright install chromium`.

## Dependencies

Runtime libraries, from [pyproject.toml](../pyproject.toml): FastAPI, Uvicorn, Jinja2, python-multipart, SQLAlchemy, httpx, BeautifulSoup, tldextract, APScheduler, whoisit, python-whois, Playwright, itsdangerous. The project requires Python `>=3.12`. The package name is `domain-monitor`. The version in the `[project]` table is what the git hooks read.

Declared package data is `templates/*.html`, `static/*`, and `static/fonts/*`. A non-editable install (`pip install .`, which is what the image does) includes the three `.woff2` files and their license texts. An editable install serves the same files from the source tree.

## Failure banners

[app/notices.py](../app/notices.py) keeps process-local messages. They reset on restart. The base template loads them on each page and HTMX replaces them from `GET /notices` every 30 seconds.

| Banner | Cause |
| --- | --- |
| `Screenshots are unavailable: …` | Chromium failed to launch. Cleared after a later launch succeeds. |
| `Screenshot storage failed: …` | A JPEG could not be written. Cleared after a later write succeeds. |
| `Registration lookup failed for {hostname}: …` | That monitor's latest registration snapshot failed. A later success for a different monitor leaves this line in place. The line goes away when this monitor's own latest snapshot succeeds. |
| `Scheduler has not run in over a minute.` | The scheduler is enabled, and either no tick has been recorded within 60 seconds of start, or the last tick is older than 60 seconds. |

Per-domain states are stored on the monitor and are separate from these banners. The list shows **Pending** before the first result, the status code or a short error, **Partial** when the HTTP result was saved and screenshot or crawl did not finish, and **Overdue** when the last completed check is older than the interval plus one minute while no check is running. **Recent problems** lists up to 10 component failures from the latest 40 events: a missing status with an error, an HTTP status of 400 or higher, a failed screenshot, or a failed or partial crawl.

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
