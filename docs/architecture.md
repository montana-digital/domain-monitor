# Architecture

Domain Monitor is one operating-system process. [app/main.py](../app/main.py) builds a FastAPI application that serves HTML, and the same process runs APScheduler, httpx probes, Playwright Chromium, and SQLite writes. There is no second service to deploy.

Importing `app.main` calls `create_app()`, which reads the environment, creates the data directory, and opens the database before the server accepts connections. Docker starts Uvicorn on `app.main:app`. `python -m app.main` uses the host and port from settings.

## Startup

`Settings.from_env()` in [app/config.py](../app/config.py) resolves `DATA_DIR` (default `./data`), creates that directory, and loads the session secret. [app/db.py](../app/db.py) `init_db` then:

1. Creates `DATA_DIR` and `DATA_DIR/screenshots`.
2. Opens `DATA_DIR/monitor.db` with SQLAlchemy. `check_same_thread` is false because request handlers and check tasks share the engine.
3. On each connection sets `journal_mode=WAL`, `busy_timeout=5000`, and `foreign_keys=ON`.
4. Runs `Base.metadata.create_all`. There is no migration tool. A new table or column in [app/models.py](../app/models.py) is created on an empty database. An existing database is left as it is when a column is added later.
5. Inserts the single `app_settings` row (`id=1`, retention 30 days) when it is missing.

The ASGI app then adds middleware and routes:

- `SecurityHeaders` appends `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin` on HTTP responses.
- `SessionMiddleware` signs the session cookie with the secret. `same_site` is `lax`, `https_only` follows `DOMAIN_MONITOR_HTTPS_ONLY` (off by default), and `max_age` is 14 days.
- The page router from [app/routes/pages.py](../app/routes/pages.py) is included. Every route on that router depends on `require_user`.
- `/static` is mounted from `app/static` after the router. The mount is outside the router, and `require_user` also skips paths that start with `/static`.
- Exception handlers render the login redirect, the 404 and 403 pages, and a generic 500 page. Unhandled exceptions are logged and the response body is the 500 template, or the plain sentence "Something went wrong." if rendering that template fails.

Logging is basic `INFO` to stderr when the root logger has no handlers yet.

## Lifespan

The FastAPI lifespan in `create_app` is the process lifecycle.

When `settings.start_scheduler` is true (the default):

1. `prepare_schedule()` loads monitors whose `next_check_at` is null or already due, ordered by id. It spreads those due times across 30 seconds (`30 / count` seconds between neighbors, or no delay when only one is due) and commits. This avoids a thundering herd after a restart.
2. `notices.scheduler_enabled` is set true.
3. `start_scheduler()` starts an `AsyncIOScheduler` job named `tick` every 15 seconds, with `max_instances=1` and `coalesce=True`. It also schedules one immediate `tick` on the running loop.

When `start_scheduler` is false, the banner system records that the scheduler is off and no job is started. Tests use that flag. Production settings always start the scheduler.

On shutdown, in order:

1. `begin_shutdown()` makes new enqueues return false.
2. If the scheduler was started, it is stopped with `wait=False`.
3. `shutdown_checks()` waits up to 25 seconds for check and registration tasks tracked in the in-memory task set, then cancels whatever is still running and waits one more second.
4. `shutdown_browser()` closes Chromium and stops Playwright.

Registration lookups are in that same task set. Cancelling the task releases the registration slot. A registry call or `getaddrinfo` that is already blocked in a worker thread can still run until it returns; the asyncio task does not wait for that thread.

## The tick

[app/scheduler.py](../app/scheduler.py) `tick` does three independent pieces of work. An exception in one is logged and does not skip the others. The tick always stamps `notices.last_tick` at the end, which is how the UI decides the scheduler is alive.

1. **Due monitors.** `due_monitor_ids()` selects `monitors.id` where `next_check_at` is null or `<= now`. Each id is passed to `try_enqueue(..., "schedule")`. A scheduled enqueue stops once eight checks are occupying a slot, so a large due set does not create one task per monitor. Monitors left out stay due and are picked up on a later tick. Check now is not part of that cap.
2. **Registration.** `registration_due_ids()` is one query: every monitor outer-joined to `max(fetched_at)` grouped by monitor. A monitor is due when it has no snapshot or the latest fetch is older than 24 hours. Each id is passed to `try_enqueue_registration`.
3. **Retention.** `maybe_retain()` deletes expired screenshot files at most once an hour. See [data-model.md](data-model.md).

The due-monitor query, the registration query, and the retention sweep run in a worker thread. Creating the tasks stays on the event loop.

`queue_ids` swallows and logs an exception from a single enqueue so one monitor cannot stop the rest of the list.

## How a check is queued

State lives in module globals in [app/service.py](../app/service.py):

| Name | Role |
| --- | --- |
| `_inflight` | Monitor ids whose HTTP check task has been created and not finished. |
| `_occupying` | Those ids that currently hold a check slot, or are queued to take one. A check drops out of this set while it waits for Chromium. |
| `_reg_inflight` | Monitor ids whose registration task has been created and not finished. |
| `_tasks` | The asyncio tasks for HTTP checks and registration lookups. Shutdown waits on this set. |
| `_shutting_down` | When true, enqueue functions return false. |
| `_check_sem` | `asyncio.Semaphore(8)`, recreated if the event loop changes. |
| `_reg_sem` | `asyncio.Semaphore(2)`, same loop rule. |

`try_enqueue(monitor_id, trigger)` returns false when the process is shutting down, the id is already in `_inflight`, there is no running loop, or the trigger is `schedule` and eight checks already occupy a slot. Otherwise it adds the id, creates `_guarded`, and tracks the task. The route that adds a domain and the Check now button both call this, so the first check does not wait for the next 15-second tick. The trigger string stored on the event is `schedule` or `manual`. Check now still enqueues when the schedule cap is full.

`_guarded` takes a check slot, calls `_execute_and_save`, logs unexpected crashes, and always removes the id from `_inflight` and `_occupying`. For the `capture_views` call it releases that slot and takes it again before crawl and save. Probes can use the eight slots while captures wait on the browser lock.

Registration uses the same shape with `_reg_inflight` and the registration semaphore, and its task is added to `_tasks`. The lookup itself runs in a worker thread because `whoisit` and `python-whois` are blocking. The wait is 30 seconds. A timeout stores a failed snapshot (`Registration lookup timed out.`) and releases the slot.

## Request path

A browser request hits Uvicorn, then `SecurityHeaders`, then `SessionMiddleware`, then either a page route or `/static`.

`require_user` allows `/login`, `/health`, and any path starting with `/static` through. If `DOMAIN_MONITOR_PASSWORD` is unset, every other route is open. If it is set, the session must contain `auth`. Otherwise the handler raises `LoginRequired` and the exception handler redirects to `/login?next=...`. `safe_next` in [app/presentation.py](../app/presentation.py) keeps that path only when it starts with a single `/`.

State-changing forms post URL-encoded bodies (the CSV preview posts `multipart/form-data`). Each of those handlers checks the CSRF token before it reads the rest of the form. See [security.md](security.md).

Page handlers open a SQLAlchemy session with `session_scope`, which yields one session and closes it. Check tasks open their own short sessions through `SessionLocal` and do not share a request session. `expire_on_commit` is false.

HTML is rendered by [app/templating.py](../app/templating.py). Jinja autoescape is on. The monitor list and the banner strip are also returned as fragments for HTMX. See [ui.md](ui.md).

`GET /health` does not require a session. It returns `{"status": "ok"}` with status 200 when `SELECT 1` succeeds and the scheduler is not stalled. Otherwise it returns status 503 and `{"status": "error"}`, adding `database: unavailable` when the ping fails and `scheduler: stalled` when the scheduler is enabled and `last_tick` is missing for more than 60 seconds after start or is older than 60 seconds. A disabled scheduler is not a stall.

## What one check does inside the semaphore

`_execute_and_save` loads a `MonitorSpec`: hostname, registrable domain, interval, the four option flags, and the enabled proxy rows when `use_proxy` is true. It then calls `execute_check` in [app/checks/runner.py](../app/checks/runner.py) with a 180-second deadline. The probe, capture, and crawl callables default to the real HTTP prober, Playwright capture, and crawler. Tests pass fakes.

The outcome is written in `_save_outcome`. A crash before an outcome exists is written by `_save_crash` as a finished event whose error is `Check failed: unexpected error`, and the monitor's next check is still pushed forward by its interval. One monitor's exception is caught inside `_guarded`, so the next queued monitor still runs.

The pipeline itself is [check-pipeline.md](check-pipeline.md).

## In-memory notices

[app/notices.py](../app/notices.py) holds process-local strings. They are not in SQLite and reset when the process restarts.

| Field | Set when |
| --- | --- |
| `browser_error` | Chromium fails to launch. Cleared after a later successful launch. |
| `disk_error` | A screenshot JPEG cannot be written. Cleared after a later successful write. |
| `last_tick` | Every scheduler tick, after the work. |
| `scheduler_enabled` | Lifespan, from `start_scheduler`. |

`banners()` adds a scheduler warning when `scheduler_stalled()` is true: the scheduler is enabled, and either no tick has been recorded within 60 seconds of process start, or the last tick is more than 60 seconds old. Registration failures are not stored here. The page and the `/notices` fragment append one line per monitor whose latest registration snapshot failed, read from SQLite at render time. A success for one monitor does not hide a failure for another. The browser, disk, and scheduler lines describe the process.

## Clocks

[app/clock.py](../app/clock.py) stores timezone-aware UTC. `as_utc` treats a naive datetime as UTC. Intervals, overdue checks, registration age, and retention all use these helpers. The UI prints `YYYY-MM-DD HH:MM UTC`.

## Boundaries that matter for a change

- The HTTP app, the scheduler, and Chromium share one event loop. Page handlers call synchronous SQLite on that loop. The tick's due-monitor query, registration query, and retention sweep, including screenshot file deletes, run in a worker thread. Registration lookups and `getaddrinfo` also run in a worker thread, with a 30-second and a 5-second wait. A timed-out DNS check is `DNS failed`. New blocking registry or DNS work should use a thread and a deadline as well.
- Screenshot capture holds a process-wide lock, so captures still run one at a time. A check releases its check slot for that wait, then takes a slot again for crawl and save.
- SQLite is the only store. WAL and a 5-second busy timeout are the concurrency strategy. `_save_outcome` retries a busy commit once after 0.2 seconds and deletes any JPEG it wrote for the failed attempt. If the second commit is still busy, it writes one event that keeps the measured status and says `Result could not be stored.`, and it moves `next_check_at` forward. `commit_with_retry` logs a busy error and re-raises. There is no second database.
- `create_all` will not alter an existing table. A schema change needs a deliberate migration step or a new database. See [data-model.md](data-model.md).
- Settings are read once at process start. Changing `DOMAIN_MONITOR_PASSWORD` or `DATA_DIR` requires a restart. Retention days and the proxy pool are read from SQLite on each use.
