# Agent guide

Read this file before changing Domain Monitor. The design record in [docs/](docs/README.md) describes the process as it runs today. The operator quickstart is [README.md](README.md).

Do not read or copy `data/` into docs, commits, or chat. That directory holds `monitor.db`, `secret.key`, and screenshots.

This is a single Uvicorn process with SQLite, an in-process scheduler, and local Chromium. It is not a Netlify or Render app. Do not add a platform config for those hosts unless the operator asks to change how it is deployed.

## Where to start

| Task | Read first | Then open |
| --- | --- | --- |
| Startup, shutdown, scheduler, concurrency | [docs/architecture.md](docs/architecture.md) | [app/main.py](app/main.py), [app/scheduler.py](app/scheduler.py), [app/service.py](app/service.py) |
| Tables, files, retention | [docs/data-model.md](docs/data-model.md) | [app/models.py](app/models.py), [app/db.py](app/db.py) |
| HTTP probes, vantages, crawl, screenshots, registration | [docs/check-pipeline.md](docs/check-pipeline.md) | [app/checks/](app/checks/) |
| Auth, CSRF, SSRF, cookies, proxies | [docs/security.md](docs/security.md) | [app/safety.py](app/safety.py), [app/routes/pages.py](app/routes/pages.py), [app/config.py](app/config.py) |
| RDAP, WHOIS, proxies, what is not called | [docs/external-services.md](docs/external-services.md) | [app/checks/registration.py](app/checks/registration.py), [app/domain_parser.py](app/domain_parser.py) |
| Ubuntu launcher, environment, data directory, version hooks | [docs/operations.md](docs/operations.md) | [launch.py](launch.py), [pyproject.toml](pyproject.toml) |
| Pages, HTMX, status labels | [docs/ui.md](docs/ui.md) | [app/templates/](app/templates/), [app/presentation.py](app/presentation.py), [app/templating.py](app/templating.py) |
| Later work, one item at a time | [docs/backlog.md](docs/backlog.md) | The files named in that item |

## Constraints that are easy to miss

- Schema changes are not migrated. `create_all` creates missing tables and leaves an existing `monitor.db` alone. Say so in the change if you add a column.
- Check work shares one event loop. Blocking calls stall the UI. Registration already runs in a thread.
- At most eight checks, two registration lookups, and one screenshot capture run at once. A check budget is 180 seconds. Vantages stop at six.
- Address checks in [app/safety.py](app/safety.py) must stay on probes, crawl redirects, and screenshot navigations. New outbound fetches need the same gate.
- State-changing forms need the CSRF field. New POST routes should use `csrf_ok`.
- Screenshot paths must stay under `DATA_DIR/screenshots`.
- `trust_env=False` on httpx is intentional. A proxy is used only from the saved pool, and only when the monitor flag is on.
- The display status prefers the first `2xx` across vantages. Direct desktop is the fallback. Changing that changes what the list calls green.
- Hostname parsing uses the Public Suffix List snapshot inside `tldextract`. It does not download a list at runtime.
- After a clone, git hooks are off until `git config core.hooksPath .githooks`. A commit after the first one bumps the patch version.

## Tests

Run `pytest` from the repository root after installing `.[dev]`. Match the existing test module when you change behavior:

| Area | Module |
| --- | --- |
| Scheme fallback, retries, partial saves, deadline, shutdown | [tests/test_fallbacks.py](tests/test_fallbacks.py) |
| Check level, vantages, crawl scope, screenshot address guard | [tests/test_check_level.py](tests/test_check_level.py) |
| Hostnames, CSV, blocked addresses, proxy URLs | [tests/test_domain_parser.py](tests/test_domain_parser.py) |
| Detail-page summary and screenshot marks | [tests/test_detail_page.py](tests/test_detail_page.py) |

Update the design record in the same change when the behavior in `docs/` is no longer true.

[docs/backlog.md](docs/backlog.md) is a list of work that is not built yet. Read an item before starting it. Do not treat that file as a description of the running app. Finish one item, then update the backlog and the design record together.
