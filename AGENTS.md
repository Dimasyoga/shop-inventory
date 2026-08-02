# AGENTS.md

Orientation for the next agent working on this repo. Read this, then skim
`README.md` for the product-level tour.

## What this is

A Flask + SQLite shop-inventory app (Python 3.13) with a Jinja/vanilla-JS web UI
and a long-polling Telegram bot. Currency is Indonesian Rupiah. UI + bot are
bilingual (English / Bahasa Indonesia).

## Architecture & where things live

| File | Responsibility |
|---|---|
| `app.py` | Flask routes, request validation, session/auth. `bootstrap()` does logging + `init_db()` + `BotPoller`. |
| `wsgi.py` | Gunicorn entrypoint. Calls `bootstrap()`; importing `app:app` directly would skip it. |
| `services.py` | Business logic shared by web routes **and** the bot. Each function takes an open sqlite3 connection, owns its transaction, and raises `ServiceError`/`NotFoundError`. |
| `reports.py` | Monthly audit report: collects a month's records and renders the PDF (fpdf2). Same connection/no-Flask rules as `services.py`; imports fpdf lazily so importing the module never requires it. |
| `telegram_bot.py` | Bot API client, screen renderers, stateful order/restock flows, monthly-report delivery, and the `BotPoller` daemon thread. Must **not** import `app.py`. |
| `database.py` | Schema (`init_db`), idempotent migrations, `get_setting`/`set_setting`, DB connection. |
| `i18n.py` | `TRANSLATIONS` table (English source string → translation), `make_t(lang)`, calendar names. Shared by templates, `app.js`, and the bot. |
| `templates/*.html` | Jinja templates; `settings.html` holds Telegram/language/account config. |
| `static/js/app.js` | Client JS; `t(...)` mirrors the server translator. |
| `tests/` | pytest. `conftest.py` has `db_path` (temp DB), `client`, `insert` fixtures. |
| `tests/browser/` | Playwright against a real server. `conftest.py` has `live_server`, `shop` (seeding + stock assertions) and a signed-in `page`. |

## Key conventions (follow these)

- **Settings** are string key/value rows in the `settings` table. Read with
  `get_setting(db, key, default)`, write with `set_setting(db, key, value)`
  (does **not** commit — caller owns the transaction). Keys listed in
  `database.ENCRYPTED_SETTINGS` (currently just `telegram_bot_token`) are held
  encrypted at rest as `enc:v1:…` and must go through `get_secret_setting` /
  `set_secret_setting` instead; adding a key to that tuple also migrates any
  existing plaintext value on the next `init_db()`. Reads tolerate un-prefixed
  legacy values, and a key mismatch returns `''` with an ERROR log rather than
  raising — losing the key must never take the web UI down.
- **Timestamps** are stored as UTC `'YYYY-MM-DD HH:MM:SS'` strings (SQLite
  `CURRENT_TIMESTAMP`). Compare against `services._to_utc_str(dt)` output — fixed
  width, so lexical string comparison is chronological.
- **i18n:** any user-facing string is an English literal wrapped in `t(...)` —
  including API error/warning text, which the browser shows verbatim in a toast
  (use `app._err('English source', **params)` instead of `jsonify({'error': ...})`).
  Add the Indonesian value to `TRANSLATIONS['id']` in `i18n.py`; missing keys
  fall back to English, and `tests/test_i18n_coverage.py` scans every call site
  to fail the suite when one is absent. Date/month labels come from
  `i18n.month_name` / `weekday_abbr`, never `strftime('%b')`. Bot config
  (language, whitelist, token, timezone, thresholds) is re-read every poll cycle,
  so web-UI changes apply with no restart.
- **Every stock movement records who caused it.** `stock_logs.actor` is
  `web:<username>` from a route (`app._actor()`), `telegram:<chat_id>` from the bot
  (`telegram_bot._actor(chat_id)`), or `services.ACTOR_SYSTEM` for anything with no
  request behind it. The five service functions that write a movement take a
  keyword-only `actor=ACTOR_SYSTEM`; the default is deliberately honest rather than
  convenient, because crediting an unattributed call to the admin would make the
  column worse than useless the one time somebody reads it. Rows from before the
  column stay `NULL` and are **not** backfilled, so the history shows those as unknown
  rather than inventing an actor. The Stock History page (`/stock-history`,
  `services.list_stock_movements`) is what reads the table back. `reason` is stored
  English written at the time of the movement and is displayed **as recorded, never
  translated** — it is a record of what happened, and it is not a `t(...)` call site.
- **Lists are paged, and a page costs a fixed number of queries.** `list_orders`
  returns `(orders, has_more)` where each order is a dict already carrying its `items`,
  fetched for the whole page in one `IN` query — never one per row. `GET /api/orders`
  returns `{orders, has_more, page}`, and a single order comes from
  `GET /api/orders/<id>`: pulling the list and searching it client-side breaks the
  moment the order is not on the page being held. Order by `created_at DESC, id DESC`,
  not `created_at` alone — the column is second-resolution, and an unstable tiebreak
  lets a row cross the page boundary between requests and be shown twice or skipped.
  `tests/test_orders_pagination.py` pins the query count with sqlite3's trace hook.
  The restock and self-use histories follow the same contract through one shared
  implementation, `services._list_batches` — `{batches, has_more, page}`, two queries a
  page, `created_at DESC, id DESC`. They are the reason the rule is written down: both
  were read whole with no `LIMIT` anywhere and then fanned a query out per batch for its
  lines, so opening `/restock` cost a query per batch the shop had ever recorded and
  shipped the lot to the browser as one array, which the page rendered into a single
  `innerHTML`. At two years of a thousand orders and three hundred restocks a month
  that measured 7,201 queries and 5.8 MB to show ten rows.
  `tests/test_batch_history_pagination.py` is parametrized over both, because they are
  one implementation behind two endpoints and a fix that lands on only one side is the
  regression worth catching.
- **Indexes live in one `executescript` at the very end of `init_db()`**, after every
  migration, because several cover columns the `ALTER TABLE` blocks above add — build
  them earlier and an upgrade of an old database fails on a column that does not exist
  yet. `CREATE INDEX IF NOT EXISTS` makes the block idempotent on its own, so it needs
  no `PRAGMA` guard. SQLite indexes primary keys and UNIQUE columns by itself but *not*
  foreign keys, which is what every one of these is for. `tests/test_indexes.py` asserts
  on `EXPLAIN QUERY PLAN`, not on timings: the shop's own database is small enough that
  a scan is invisible, so an index the planner declines to use would cost writes and buy
  nothing with no test noticing. Adding one means adding the plan assertion that
  justifies it. `stock_logs` carries exactly one index, `(product_id)`, and that is a
  ceiling rather than a starting point: it is the table every sale, restock and self use
  writes to. It stays that narrow because an index entry carries the rowid and
  `stock_logs.id` *is* the rowid, so `(product_id)` alone already orders a product's
  movements the way the history page reads them — `ORDER BY id DESC`, never
  `created_at DESC`, which would buy a sort and a wider index for nothing. The
  unfiltered history has no index at all on purpose: descending rowid walks the table
  backwards and stops at the page size.
- **`api()` in `app.js` rejects on any non-2xx, so every caller needs a `.catch`.**
  Write `api(...).then(d => ...).catch(err => showToast(err.message, 'error'))`.
  `.then(d => d.success ? ... : showToast(d.error))` is the trap: `api()` throws rather
  than resolving with the error body, so that failure branch is unreachable and the
  rejection goes nowhere. Eight write paths were written that way and silently swallowed
  every refusal the server made — a rejected order edit left the modal sitting open with
  no explanation. `tests/browser/test_error_reporting_ui.py` pins the behaviour; the
  server-side tests cannot see it, because the API was returning a correct 400 the whole
  time. Anything rendering into the page rather than acting on the result should use
  `fetchJson`, which toasts on failure by itself.
- **UI behaviour is tested in `tests/browser/`, in a real browser.** `static/js/app.js`
  is a thousand lines that no Flask test client can reach: paging, modal state and
  downloads are all client-side, and three features once shipped with a green suite and
  had to be clicked by hand before anyone knew they worked. The server there is a
  **subprocess**, because `tests/conftest.py` monkeypatches `database.DB_PATH` and a
  threaded server would share that global. Sign-in mints a session cookie rather than
  driving the login form — it keeps the suite off the login throttle, which buckets by
  address and would lock out after five tests. Reach for these whenever a change lands
  in `app.js` or a template; assert on what the reservation columns did afterwards
  (`shop.stock_of`), not just on what the page says.
- **Migrations** live in `init_db()` and must be idempotent (guard with
  `PRAGMA table_info` / `sqlite_master` checks). New columns go both in the
  `CREATE TABLE` block *and* a guarded `ALTER TABLE` for existing DBs. *Removing* a
  foreign-keyed column means rebuilding the table — SQLite rejects `DROP COLUMN` on
  one named in a constraint. Follow the categories-removal migration: rename the old
  table aside under `legacy_alter_table=ON` so the foreign keys in `order_items`,
  `stock_logs` and friends are not rewritten to follow it, recreate the table under
  its real name, copy rows **with their ids**, then verify with
  `PRAGMA foreign_key_check` and refuse to continue if anything dangles.
  `tests/test_migrations.py` builds an old-shaped database and asserts the outcome;
  extend it rather than trusting a destructive migration by inspection.
- **Corrections are reversing entries, never edits.** A mistyped restock or self-use
  batch is voided by `services.void_restock` / `void_self_use`, which write a second
  batch holding the negated figures and linked by `voids_batch_id`. That one column
  carries both halves: a batch *is* a void when its own is set, and *has been* voided
  when another points at it. Nothing is deleted or rewritten, so `sales_summary` and the
  monthly report net the pair out on their own and a month already reported keeps the
  totals it printed. Any new query over batches must skip rows whose batch has been
  voided — `services._surviving_restock_lines` is the predicate.
- **`cost_price` can only be un-blended from a snapshot.** `create_restock` records
  `restock_items.cost_before` per line; voiding restores it, but *only* when no later
  surviving batch has averaged onto it, since rebuilding the weighted average would need
  stock levels that sales have since moved. When it cannot, the product gets
  `products.cost_review_needed = 1` rather than an invented figure, and the Products page
  *Needs cost* chip (`services.NEEDS_COST`) asks for a human. Resist any change that
  makes the flag clear itself — only an explicit cost on the product form does, because
  a later restock blends onto the suspect base and inherits the doubt.
- **An open order holds its stock; `stock_qty` never moves until it is completed.**
  `products.reserved_qty` counts units promised to draft and confirmed orders, and what
  a new order may draw on is `services.AVAILABLE` (`stock_qty - reserved_qty`), never
  `stock_qty`. Keep the two apart: `stock_qty` is physical stock, which is what the
  dashboard, low-stock alerts, the restock weighted average and the monthly report all
  read, and none of them should shift because an order was typed. Hold with
  `_hold_stock` — the condition and the increment are one statement, because checking
  first and claiming after is the exact race this replaced. Completion decrements both
  columns together; cancelling releases. Releases clamp at zero, since a negative
  reservation would read as extra availability and hand out stock that isn't there.
- **Editing a draft releases every old line before taking the new ones**
  (`services.update_order`), rather than computing per-product deltas. That ordering is
  what lets an edit spend units it is itself giving up — dropping one line to add
  another of the last item, or just correcting 3 to 2 — and `db.rollback()` restores the
  old holds exactly when a new line cannot be met. Drafts only: a confirmed order has
  been paid for. Self use and stock adjustment still work on `stock_qty` directly and
  are *not* checked against reservations — they record something that already physically
  happened, so refusing them would only make the database wrong. A hold is a claim on
  stock, not a lock on the shelf, and `complete_order` keeps its `stock_qty` guard for
  exactly that case. Because `reserved_qty` is a counter and the open order lines are
  what justify it, `services.reservation_drift` recomputes and compares the two;
  `repair_reservations` resets the counter and returns the *before* state so the caller
  can report what changed. Startup logs drift and never repairs it, and the two are
  separate actions in Settings on purpose — the figure says what customers were
  promised, so it gets shown before anything rewrites it. Resist making repair
  automatic for the same reason `cost_review_needed` does not clear itself.
  `_DRIFT_SQL` aggregates the open order lines **once** and left-joins that onto
  products — never a correlated subquery per product, which is what it was: finding the
  few lines still open meant walking every line each product had ever sold, so the check
  cost grew with the shop's whole order history (194 ms at two years of a thousand
  orders a month — the slowest endpoint in the app, and the same query `bootstrap()`
  runs at startup). What it should depend on is how many orders are *open*, and those do
  not accumulate. `tests/test_reservation_drift.py` asserts on the plan, not a timing.
- **Sessions are an idle window, and API routes answer 401 rather than redirecting.**
  `app.permanent_session_lifetime` is `SHOP_SESSION_HOURS` (default 12) and sign-in sets
  `session.permanent = True`; Flask checks the window against the cookie's own signature
  on every request, so it holds server-side, and `SESSION_REFRESH_EACH_REQUEST` re-issues
  the cookie each response so working restarts the clock. An absolute limit would sign
  the seller out mid-order. `app._unauthenticated()` splits on the `/api/` prefix: a page
  gets the login screen, a fetch gets 401 JSON, because a redirect returns 200 and an
  HTML body that `api()` then fails to parse — reporting a JSON syntax error to someone
  whose actual problem is that they need to sign in. `goToLogin()` in `app.js` handles
  the 401 by navigating and returning a promise that **never settles**, so no caller's
  `.catch` toasts against a page already leaving. Any new client-side `fetch` must go
  through `api`/`fetchJson` or repeat that 401 check.
- **The login throttle buckets by client address, not username.** Five failures in
  15 minutes lock the bucket (`app._login_failures`), and a lockout refuses the
  *correct* password too — checking credentials first would make the throttle an
  oracle for whether a guess was right. Keying it on the username instead would
  hand anyone a way to lock the shop owner out of their own shop, which is why it
  is not. The map is process memory, justified by the one-worker rule below;
  `tests/conftest.py` clears it around every test, since the process outlives them
  and every test client shares one address.
- **`ServiceError`** carries an English `template` + `params` for translation via
  `i18n.translate_error`; `str(e)` still yields English for logs.
- **The bot poller** advances its update offset even when handling an update
  throws, so a poison update never loops.
- **A month is rendered once, then served from the archive.** Collecting a month costs
  milliseconds; rendering it costs *seconds* — fpdf2 measures every cell, so the bill
  grows with the month's sale lines and reached ~6 s at a thousand orders. That is six
  seconds of CPU in a server pinned to one worker, blocking every other request and,
  through the GIL, the bot poller — and `build()` used to pay it again on every download
  of the same closed month. It now hashes the collected data plus the language
  (`reports.fingerprint`) into a `.sha256` sidecar beside the PDF and reuses the archived
  bytes when the hash still matches. Do **not** replace this with "closed months never
  change": an order created in June and completed in July moves June's figures, which is
  exactly what the fingerprint catches and a date rule would not. `generated_at` is the
  one key left out — it moves on every `collect()` and would make the hash always miss;
  a served archive keeps the timestamp of the render that really produced it, which is
  what the line claims. The stamp is written *after* the PDF, so a crash between them
  leaves no stamp and costs a re-render rather than validating a file that isn't there.
- **Recurring bot work** hangs off `_cycle` behind a monotonic-deadline check
  (`_maybe_check_alerts`, `_maybe_send_report`), each with its own DB connection
  and a blanket `except Exception: log.exception(...)` so a failing job cannot kill
  the poller. Anything firing less often than the process restarts needs its
  progress **persisted** — the monthly report keys off a `last_report_period`
  settings row, because an in-memory deadline re-fires on every restart. A marker
  that is absent means "no history", and must plant itself without acting rather
  than backfilling.

## Running things

```bash
bash start.sh            # or: python3 app.py  (serves http://localhost:5000)
ruff check .             # config in ruff.toml; CI fails on a finding
./backup.sh              # backups/shop-<stamp>.db; ./restore.sh <file> puts one back
source venv/bin/activate && python -m pytest -q   # full suite, browser tests included
playwright install chromium    # one-off, before the browser tests can run
python -m pytest -q -m "not browser"   # skip them if you have not
docker compose up -d --build   # deployment path; ./deploy.sh to update
```

Default login `admin` / `admin123` (override with `SHOP_ADMIN_USERNAME` /
`SHOP_ADMIN_PASSWORD` before first start). `FLASK_DEBUG=1 ./start.sh` enables the
Werkzeug debugger (never on an untrusted network).

## Deployment constraints (don't break these)

- **One worker, always.** `BotPoller` is an in-process thread holding a single
  `getUpdates` offset and the store is one SQLite file, so a second worker or
  replica duplicates every bot message and doubles stale-order alerts. Scale with
  `--threads`. This is why the Dockerfile pins `--workers 1`. A second reason has
  since been measured: `app._login_failures` is process memory, so N workers give an
  attacker 5×N attempts before any bucket locks — going multi-worker means moving the
  throttle into shared state *first*, not as a follow-up. See the limits below before
  reaching for workers to fix a latency problem; the one that motivates it usually
  is not one.
- **State lives outside the source tree.** `SHOP_DB_PATH` and
  `SHOP_SECRET_KEY_PATH` point into the `/data` volume. Keep `database.DB_PATH` a
  module-level name read at call time inside `get_db()` — `tests/conftest.py`
  monkeypatches it, and moving the lookup into a config object breaks the suite.
- **Anything a deployment must set goes in `.env.example`**; anything the shop
  owner should change while running stays a `settings` row (re-read every poll
  cycle, so no restart).
- `/healthz` is unauthenticated and returns untranslated JSON on purpose — it is
  the container healthcheck, and `t(...)` there would trip the i18n coverage test.
- New runtime assets must be vendored into `static/`, not pulled from a CDN: the
  shop's network may be offline. That covers the PDF fonts in `static/fonts/` as
  much as Chart.js — and they must stay Unicode-capable, since product names are
  user data and a Latin-1 font raises on the first curly quote.
- **Generated files never go in the source tree.** `reports.REPORT_DIR` follows the
  `database.DB_PATH` idiom (module-level, env-overridable, read at call time) and
  points into `/data` in Docker; `tests/conftest.py` redirects it to `tmp_path` so
  no test can leave a PDF behind.

## Measured limits (so nobody re-optimizes what is already fine)

Taken against a synthetic database of two years at a thousand orders and three hundred
restocks a month — 24k orders, 60k order lines, 7.2k restock batches, 82k stock logs,
13.7 MB — with the app under real gunicorn (`--workers 1 --threads 8`). Numbers here
exist to stop the next person spending a week on the wrong thing; re-measure before
trusting them against a much larger shop.

- **The monthly PDF render is the real ceiling, and it is the only one.** ~2.1 ms per
  sale line, linear and confirmed over a 16× range: 621 lines → 1.6 s, 2,493 → 5.3 s,
  10,035 → 21.1 s. Extrapolated, it reaches gunicorn's `--timeout 60` at roughly 28,000
  sale lines, or about **11,000 completed orders a month** — where the worker is killed
  mid-render and the download simply fails. That is the point at which the render has to
  move off the request thread (a job the page polls, or the poller pre-building the
  month). Nothing else in the app is within an order of magnitude of breaking, and the
  fingerprint archive already means a month is normally rendered once rather than per
  download.
- **A long render does *not* block the app**, which is worth knowing because it reads
  like it should. Measured during a 6.15 s render: 225 concurrent requests completed,
  median 2.1 ms against a 1.8 ms baseline, p95 285 ms, max 391 ms. Python switches
  threads often enough that a CPU-bound request degrades tail latency rather than
  stopping service. Do not reach for `--workers` over this — see the login throttle note
  above for what that would cost.
- **The SQLite pragmas are already right; leave them.** On the real disk (ext4/nvme,
  *not* the tmpfs a scratch benchmark lands on): raising `cache_size` from the 2 MB
  default to 16 MB or 64 MB changed the sales-page queries by nothing at all
  (52.9 → 53.1 ms), because the cost is CPU grouping rows, not I/O. `ANALYZE` likewise
  changed nothing — the plans already pick the right indexes. `synchronous=NORMAL` *is*
  a real 25× on writes (5.17 ms → 0.21 ms per order), and is still the wrong trade: at a
  thousand orders a month that is 1.4 writes an hour, so it buys latency nobody can
  perceive and pays for it by making a power cut able to lose committed orders. `FULL`
  stays.
- **Deep pagination is not a problem.** Orders page 2,300 is 1.0 ms and stock movements
  page 3,000 is 12.7 ms; every list walks an index backwards and stops at the page size.
  Keyset pagination would buy nothing.
- **Known and accepted:** `/api/sales/product-performance?unit=year` is ~47 ms, five
  aggregate passes over one window where two would do. Collapsing them means
  restructuring the costed-vs-uncosted-line semantics that `top_products_by_profit`,
  `sales_missing_cost` and the monthly report all share — a real risk to the profit
  figures for 28 ms on a page that already renders in 47. `?unit=year` on the trend
  endpoint is ~33 ms for the same reason and is bucketed in Python deliberately (see the
  timezone note in `api_sales_trend`).
- **Catalogue size is a different axis from order volume** and this shop is not near it.
  At 5,000 products (against 120 today) `/api/products` is 1.3 MB / 36 ms and the order,
  restock and self-use pages each embed ~673 KB of product JSON for their `<select>`
  pickers. Paging the products *table* is easy; the pickers genuinely need the whole
  catalogue, so the fix there is a typeahead — a UI change, not a query one.
