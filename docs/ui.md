# User interface

The UI is server-rendered HTML. [app/templating.py](../app/templating.py) loads Jinja from [app/templates/](../app/templates/) with autoescape on. Interactivity is HTML forms plus a vendored HTMX script at [app/static/htmx.min.js](../app/static/htmx.min.js). There is no frontend build and no client-side framework.

Styles are [app/static/style.css](../app/static/style.css). Fonts are self-hosted variable files for Inter, Newsreader, and JetBrains Mono under `app/static/fonts/`, each with its OFL license text. The base template does not load a CDN.

## Shell

[app/templates/base.html](../app/templates/base.html) sets `noindex`, the favicon, and a skip link. The header brand links to `/`. Primary navigation is Monitors (`/`), Add domains (`/domains/new`), and Settings (`/settings`). `section_for` in [app/presentation.py](../app/presentation.py) marks the current item with `aria-current="page"`.

The theme control is two buttons, Noir and Paper. The choice is `localStorage` key `tn-theme`. `light` sets `data-theme="light"` on the document element. Any other value removes the attribute, which is the dark theme. A tiny inline script in the head applies the stored choice before paint. The choice is per browser, not per account.

When a password is configured, the header includes a logout form with the CSRF field. Login and the error pages used for a failed login hide the navigation (`show_nav` false).

Flash messages from the session render once, then `pop_flash` clears them. Banners render above the main content.

## Live refresh

Two fragments poll the same origin:

| Element | Request | Interval |
| --- | --- | --- |
| `#live-banners` in the base template | `GET /notices` | 30 seconds |
| `#monitor-live` in [app/templates/monitors_live.html](../app/templates/monitors_live.html) | `GET /fragment/monitors` | 15 seconds |

`/notices` returns the banner partial only. `/fragment/monitors` returns the table, the empty state, and Recent problems. The rest of a page does not refresh itself. The detail page stays still until the browser reloads. Check now redirects back to the detail page with a flash message. A check that is already in flight flashes `A check is already running for this domain.`

Both fragment routes still go through `require_user`.

## Routes

Handlers are in [app/routes/pages.py](../app/routes/pages.py). POST bodies are forms. CSV preview is `multipart/form-data`. Successful writes redirect with status 303. Validation failures re-render the form with a 400. A bad CSRF token re-renders [app/templates/403.html](../app/templates/403.html) with status 403.

| Method and path | Page or result |
| --- | --- |
| `GET /health` | JSON. `{"status": "ok"}` with status 200 when the database answers `SELECT 1` and the scheduler is not stalled. Status 503 and `{"status": "error"}` otherwise, with `database` set to `unavailable` and `scheduler` set to `stalled` for each failure that applies. No session required. A disabled scheduler is not a stall. |
| `GET /login`, `POST /login` | [login.html](../app/templates/login.html). Unused when no password is set (redirects to `/`). |
| `POST /logout` | Clears the session. |
| `GET /` | [monitors.html](../app/templates/monitors.html), which includes the live table. |
| `GET /fragment/monitors` | The live table partial. |
| `GET /notices` | [notices.html](../app/templates/notices.html). |
| `GET /domains/new` | [add.html](../app/templates/add.html). |
| `POST /domains` | Create one monitor, enqueue a check and a registration lookup, redirect to the detail page. |
| `POST /domains/csv-preview` | Re-render Add with a preview. Nothing is saved. |
| `POST /domains/csv-import` | Insert the accepted preview rows, then redirect home, or back to Add when some rows were rejected. |
| `GET /domains/{id}` | [detail.html](../app/templates/detail.html). Query `page` pages history. `confirm_delete=1` opens the delete fold. |
| `POST /domains/{id}/edit` | Update interval and flags. Does not rename the hostname. |
| `POST /domains/{id}/check` | Enqueue trigger `manual`. |
| `POST /domains/{id}/delete` | Without `confirm=yes`, redirect to the confirm query. With it, delete the monitor and its files. |
| `GET /settings` | [settings.html](../app/templates/settings.html). |
| `POST /settings/retention` | `retention_days` must be 7, 30, or 90. |
| `POST /settings/proxies` | Add an HTTP or HTTPS proxy URL. |
| `POST /settings/proxies/{id}/toggle` | Flip `enabled`. |
| `POST /settings/proxies/{id}/delete` | Remove the row. Historical attempts keep their label. |
| `GET /screenshots/{id}` | JPEG for that row, or 404. |
| `GET /static/...` | Files from `app/static`. |

Unknown application URLs hit the 404 template. Other HTTP errors hit the 500 template. A failure while rendering the 500 page returns the plain text `Something went wrong.`

## Monitors

The table is ordered by hostname. Columns are Domain, Status, Check level, Interval, and Last checked. The status cell can add tags **Checking**, **Partial** (with the reason as the title), and **Overdue**.

Recent problems lists up to 10 lines from the newest 40 events. Each line links to the domain. The empty copy is `No failed checks yet.` The empty monitor list points at Add domains.

## Add domains

The shared fields live in [app/templates/toggles.html](../app/templates/toggles.html): interval select (1, 5, 15, 30, 60 minutes) and checkboxes for screenshot, crawl, proxy pool, and mobile user agent. A checkbox posts `yes`. The server treats `yes`, `on`, `true`, and `1` as true.

The single-domain form posts the raw text. The parser strips a pasted URL down to a hostname. Errors from the parser or from a duplicate hostname re-render the form.

The CSV form accepts a header row or a bare list of hostnames. Missing columns mean a 5-minute interval and every extra check off. The preview shows each row's error and a count of rows ready to add. Import posts the accepted rows as JSON and validates them again. At most 500 rows and 1 MB. The file must be UTF-8, with an optional BOM.

## Detail

The summary line repeats the status pill, the check level, and the Checking, Partial, and Overdue tags. A fact row shows the registry updated date from the last successful registration record, the last check time, and the registrable domain.

When proxy checks are on and no enabled proxy exists, a banner says so.

Folds, closed by default:

- **WHOIS / RDAP.** Pending copy until the first snapshot. On a failed latest lookup, a banner gives the time and error and, when an older success exists, says it is showing that record. Fields are registrar, created, updated, expires, nameservers, statuses, source (`rdap` or `whois`), and last lookup time.
- **Setup.** The same toggles as Add, posted to the edit route. Saving updates the interval and the flags. It does not start a check. When the new interval would make the monitor due sooner than `next_check_at`, that time moves to `last_checked_at` plus the new interval, or to now if the monitor has never finished a check. A longer interval leaves `next_check_at` where it is. Check now, and the next scheduled run, load the flags when they start, so they use the saved values.
- **Check history.** Newest first, 50 per page, with Newer and Older links. Each event is a closed `<details>` whose summary shows the time, status, screenshot and crawl marks when those options are on, desktop and mobile icons when those profiles appear on the attempts or screenshots, the trigger, the scheme, and the partial reason. The body has the attempt table (vantage, result, latency, final URL, and `Last-Modified` when present), screenshots, and a nested crawl fold (saved and failed counts, error, note, and a page table of depth, URL, result, and title).
- **Delete.** The first submit asks for confirmation. The second posts `confirm=yes`.

Screenshot marks, from `screenshot_mark`:

| Mark | When |
| --- | --- |
| saved | Outcome `succeeded` and at least one shot still has a path. |
| removed | Outcome `succeeded` and the file path is gone (retention). |
| missing | Any other outcome, including `failed` and `skipped`. |

Images use `src="/screenshots/{id}"` and an alt string that includes the viewport, hostname, and check time.

## Settings

Two cards: screenshot retention (7, 30, or 90 days) and the proxy URL field. The proxy list shows host and port, enabled or disabled, the last proxy error, and Disable or Enable plus Remove. The page states that credentials stay in the database and are not shown again.

## How status text is chosen

[app/presentation.py](../app/presentation.py) is registered on the Jinja environment together with `check_level`, `format_dt`, `json_list`, `profiles_used`, and `screenshot_mark`.

`status_text` and `status_kind`:

| Condition | Text | Kind |
| --- | --- | --- |
| No check time, no code, and no error | `Pending` | `pending` |
| No status code | The error string, or `Failed` | `bad` |
| 200–299 | The code | `ok` |
| 300–399 | The code | `warn` |
| Any other code | The code | `bad` |

`short_text` collapses whitespace and truncates with an ellipsis. The list uses 48 characters for the status and 140 for a problem line.

`is_overdue` is false while a check is running or before the first completion. Otherwise it is true when now is later than `last_checked_at + interval_minutes + 1 minute`.

`format_dt` prints `YYYY-MM-DD HH:MM UTC`, or an em dash when the value is missing.

`profiles_used` reports `desktop`, `mobile`, or both, based on attempt profiles and screenshot viewports. It ignores other strings.
